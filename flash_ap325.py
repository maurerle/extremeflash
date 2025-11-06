#!/usr/bin/env python3
'''
This script flashes a Aruba AP325 automatically to gluon.
The easiest way to do this, is by putting the u-boot.mbn and gluon factory image into a tftp folder.
You can set the ethernet port to "share connection to others" which activates DHCP as well.

Then you can start this script.
After starting the script, you plug the power cable into the router.

After a little more than 2 minutes, the flash procedure finishes.
'''

# pip install pyserial

import logging
import re

import serial

logger = logging.getLogger("ap325")

# If not set, then the DHCP gateway IP is mirrored back
TFTP_SERVER_IP = None  #'192.168.1.1'
EXPECTED_VERSION = "1.5.7.2"

def test_serial_port(potential_serial_port):
    serial.Serial(port=potential_serial_port, baudrate=115200, timeout=2)
    return potential_serial_port


def find_serial_port():
    common_serial_ports = [
        "/dev/ttyUSB1",
        "/dev/ttyUSB0",
        "COM4",
        "COM3",
        "COM2",
        "COM1",  # COM1 needs to be last as it usually always exists
    ]
    for potential_serial_port in common_serial_ports:
        try:
            test_serial_port(potential_serial_port)
            return potential_serial_port
        except serial.serialutil.SerialException as e:
            if "FileNotFoundError" in str(e) or "No such file or directory" in str(  # Windows
                e
            ):  # Linux: [Errno 2] No such file or directory: "/dev/tty.."
                logging.debug(f"Failed to access {potential_serial_port}.")
                continue
            raise
    raise RuntimeError(f"No valid accessible port found in {common_serial_ports}")

def wait_prompt(router):
    res = []
    while True:
        line = router.readline().strip()
        logger.debug(line.decode("ascii", errors="replace"))
        if line.startswith(b"apboot>"):
            break
        res.append(line)
    return res

def run_serial_command(router, cmd, wait=True):
    if isinstance(cmd, str):
        cmd = cmd.encode("ascii")
    cmd = cmd.strip()

    # Flush out any apboot> prompt that may have appeared
    router.reset_input_buffer()
    # all_data = self.read_all()
    # if all_data:
    #     logger.debug("all_data.decode("ascii"))

    router.write(cmd + b"\r\n")
    logger.debug(">>> %s", cmd.decode("ascii"))
    # The router mirrors back the command, wait for it as e.g. apboot> might appear again
    router.readline().strip()
    if wait:
        return wait_prompt(router)

def wait_for_content(router):
    line = b""
    while not line:
        line = router.readline().strip()
        # Also ignore NUL bytes, these seem to happen sometimes
        line = line.replace(b"\x00", b"")
    return line

def stop_autoboot(router, tftp_server_ip = None):
    logger.info("stopping autoboot")
    buf = b""
    while b"<Enter> to stop autoboot:" not in buf:
        new = router.read()
        buf += new
        # Throw away everything in front of the last newline
        buf = buf.split(b"\n", 1)[-1]

        assert b"Booting OS partition" not in buf, "Router is booting already, please restart"

    router.write(b"\r\n")
    wait_prompt(router)
    run_serial_command(router, "autoreboot off")
    logger.info("try get network config from DHCP")
    res = run_serial_command(router, "dhcp")
    logger.info("received DHCP settings:")
    for line in res:
        logger.info("> %s", line.decode("ascii"))
    res = b"\n".join(res)
    apboot_ip = re.search(b"^DHCP IP address: (?P<ip>.*)$", res, flags=re.MULTILINE).group("ip").decode("ascii")
    apboot_gateway = (
        re.search(b"^DHCP def gateway: (?P<gateway>.*)$", res, flags=re.MULTILINE).group("gateway").decode("ascii")
    )

    if tftp_server_ip is None:
        run_serial_command(router, f"setenv serverip {apboot_gateway}")
    else:
        run_serial_command(router, f"setenv serverip {tftp_server_ip}")
    run_serial_command(router, "setenv autostart n")

def write_netget(router, filename) -> int:
    logger.info("requesting download of %s via tftp", filename)
    res = run_serial_command(router, f"netget 44000000 {filename}")
    res = b"\n".join(res)
    uboot_size = (
        re.search(b"^Bytes transferred = (?P<bytes>.*) \\(.*$", res, flags=re.MULTILINE)
        .group("bytes")
        .decode("ascii")
    )
    return int(uboot_size)

def flash_bootloader(filename="u-boot.mbn", port="/dev/ttyUSB0", baudrate=9600):
    router_version = None
    logger.info("Power on the AP now.")
    with serial.Serial(port=port, baudrate=baudrate, xonxoff=True, timeout=5) as router:
        line = wait_for_content(router)
        m = re.match(rb"APBoot (?P<version>.*) \(build (?P<build>.*)\)", line)
        assert m is not None, f"Expected U-Boot version string, got: {line}"

        router_version = m.group("version").decode("ascii")
        logger.info(f"Found APBoot version: {router_version}")

        if router_version == EXPECTED_VERSION:
            logger.info("Nothing to do - Version matches")
        else:
            logger.info("This is not the expected version, triggering bootloader flash")
            logger.info("Version is %s, should be %s", router_version, EXPECTED_VERSION)

            stop_autoboot(router, TFTP_SERVER_IP)
            uboot_size = write_netget(router, filename)

            logger.info("writing new Bootloader - do not interrupt")
            run_serial_command(router, "sf erase 220000 100000")
            run_serial_command(router, f"sf write 44000000 220000 {uboot_size:x}")
            run_serial_command(router, "nand device 0")
            run_serial_command(router, "nand erase.chip")
            logger.info("finished writing Bootloader")
            run_serial_command(router, "reset", wait=False)

def flash_factory(filename: str, port="/dev/ttyUSB0", continue_flash=True):
    logger.info("flashing factory image")
    with serial.Serial(port=port, baudrate=115200, xonxoff=True, timeout=5) as router:

        if not continue_flash:
            line = wait_for_content(router)
            m = re.match(rb"APBoot (?P<version>.*) \(build (?P<build>.*)\)", line)
            assert m is not None, f"Expected U-Boot version string, got: {line}"

            router_version = m.group("version").decode("ascii")
            logger.info(f"Found APBoot version: {router_version}")

        stop_autoboot(router, TFTP_SERVER_IP)
        factory_size = write_netget(router, filename)

        logger.info("writing openwrt factory image")
        run_serial_command(router, "nand device 0")
        run_serial_command(router, "nand erase.part aos0")
        run_serial_command(router, f"nand write 44000000 aos0 {factory_size:x}")
        run_serial_command(router, "setenv os_partition 0")
        run_serial_command(router, "saveenv")
        logger.info("finished writing factory image")
        run_serial_command(router, "reset", wait=False)
        router.write(b"\r\n")
    logger.info("Flash procedure finished. You can now unplug the device")


def main(filename: str):
    serial_port = find_serial_port()
    flash_bootloader(filename="u-boot.mbn", port=serial_port, baudrate=9600)
    flash_factory(filename=filename, port=serial_port)

if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)-15s - %(levelname)s - %(message)s", level="INFO")
    main("gluon-ffac-v2025.0.0-9-aruba-ap-325.bin")