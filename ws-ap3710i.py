#!/usr/bin/env python3
#
#     Copyright (C) 2023 Grische
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, version 3.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-only
import ipaddress
import logging
import paramiko
import pathlib
import re
import serial
import tempfile
import tftpy
import time
from threading import Event
from threading import Thread

DRYRUN = False  # can be overriden with "--dryrun" argument
#
#  What this tool actually does
#
# 1. It will start a "TFTP server thread" on the `local_ip` on port `69`.
# 2. Starting a "serial thread" to watch and interact with the serial console.
#    First, it will interrupt the U-Boot boot process, set boot parameters for TFTP and execute the TFTP boot using
#    the initramfs-kernel.bin file.
# 3. Once the serial thread identified that the TFTP boot succeeded, an "SSH Thread" will establish an SSH connection
#    and upload the sysupgrade.bin file, run sysupgrade to install it and then terminate
# 4. In the meanwhile, the "serial thread" will watch for a message that flashing was successful and that the reboot was
#    initiated and then terminate.
# 5. In the meanwhile the main programm will wait for SSH and Serial threads to terminate and then stop the TFTP server
#
# Main Thread: .....................................................................................................
# |  |  |                                                                                           (join) /     |  \
# \  \  \_ SSH Thread: halt ....................................... resume: upload file + flash it + terminate   |   \
#  \  \                                                        (event) /                                 (join) /     \
#   \  \_ Serial Thread: Interrupt U-Boot .. Boot TFTP .. Wait for br-lan .. Wait for flash + reboot .. terminate      |
#    \                                                                                                          (stop) |
#     \_ TFTP Server Thread: ................................................................................. terminate


# TODO: these global events might need a better location
event_keep_serial_active = Event()
event_abort_ssh = Event()
event_ssh_ready = Event()


def debug_serial(string: str):
    logging.debug(string.rstrip())


def bootup_interrupt(ser: serial.Serial):
    while event_keep_serial_active.is_set():
        line = readline_from_serial(ser)

        # These lines probably only works with custom Enterasys U-Boot v2009.11.10
        # TODO: add support for other / newer versions of U-Boot
        if (
                "### JFFS2 load complete" in line  # stock firmware message
                or "### JFFS2 LOAD ERROR" in line  # OpenWRT message :-|
        ):
            text = b"x"  # send interrupt key
            logging.info(f"JFFS2 load done. Sending interrupt key {text}.")
            time.sleep(0.5)  # sleep 500ms
            ser.write(text)
            break

        time.sleep(0.01)


def bootup_login(ser: serial.Serial):
    while event_keep_serial_active.is_set():
        line = readline_from_serial(ser)

        if "[30s timeout]" in line:
            time.sleep(0.1)
            logging.info(f"Attempting to log in.")
            ser.write(b"admin\n")
            time.sleep(0.1)
            ser.write(b"new2day\n")
        elif "password: new2day" in line:
            time.sleep(0.1)  # sleep 500ms
            logging.info(f"Checking if login was successful.")
            break

        time.sleep(0.01)


def bootup_login_verification(ser: serial.Serial):
    prompt_string = "Boot (PRI)->"
    # Reading byte by byte because there is no linebreak after the prompt
    while event_keep_serial_active.is_set():
        # only read chars if there are enough bytes in wait from the buffer
        if ser.in_waiting > len(prompt_string):
            chars = ser.read(ser.in_waiting).decode('ascii')
            debug_serial(chars)

            if prompt_string in chars:
                logging.info(f"U-Boot login successful!")
                break
            else:
                raise RuntimeError("U-Boot login failed :((")

        time.sleep(0.01)


def bootup_set_boot_openwrt(ser: serial.Serial):
    if not event_keep_serial_active.is_set():
        return
    ser.write(b'printenv\n')
    time.sleep(1)
    printenv_return = ser.read(ser.in_waiting).decode('ascii')
    debug_serial(printenv_return)
    boot_openwrt_params = b'setenv bootargs; cp.b 0xee000000 0x1000000 0x1000000; bootm 0x1000000'
    if "boot_openwrt" in printenv_return:
        logging.debug("Found existing U-Boot boot_openwrt parameter. Verifying.")
        existing_boot_openwrt_params = re.search(r'boot_openwrt=(.*)\r\n', printenv_return).group(1)
        if boot_openwrt_params.decode('ascii') != existing_boot_openwrt_params:
            error_message = f'''
                    Aborting. Unexpected param for 'boot_openwrt' found.
                    Found: "{existing_boot_openwrt_params}"
                    Expected: "{boot_openwrt_params.decode('ascii')}"
                '''
            raise RuntimeError(error_message)

        logging.debug("Existing U-Boot boot_openwrt parameter looks good.")

    else:
        logging.info("Did not find boot_openwrt in U-Boot parameters. Setting it.")
        write_to_serial(ser, b'setenv boot_openwrt "' + boot_openwrt_params + b'"\n')
        time.sleep(0.5)

        write_to_serial(ser, b'setenv bootcmd "run boot_openwrt"\n')
        time.sleep(0.5)

        if DRYRUN:
            logging.info("dryrun: Skipping saveenv")
            return

        ser.write(b'saveenv\n')
        time.sleep(2)
        saveenv_return = ser.read(ser.in_waiting).decode('ascii')
        debug_serial(saveenv_return)

        if "Writing to Flash" not in saveenv_return:
            raise RuntimeError("saveenv did not successfully write to flash")


def boot_via_tftp(ser: serial.Serial,
                  tftp_ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface,
                  tftp_file: str,
                  new_ap_ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface):
    new_ap_ip_str = str(new_ap_ip.ip).encode('ascii')
    new_ap_netmask_str = str(new_ap_ip.netmask).encode('ascii')
    tftp_ip_str = str(tftp_ip.ip).encode('ascii')

    write_to_serial(ser, b'setenv ipaddr ' + new_ap_ip_str + b'\n')
    write_to_serial(ser, b'setenv netmask ' + new_ap_netmask_str + b'\n')
    write_to_serial(ser, b'setenv serverip ' + tftp_ip_str + b'\n')
    write_to_serial(ser, b'setenv gatewayip ' + tftp_ip_str + b'\n')
    logging.info("Starting TFTP Boot.")
    write_to_serial(ser, b'tftpboot 0x1000000 ' + tftp_ip_str + b':' + tftp_file.encode('ascii') + b'; bootm\n')
    max_retries = 2
    cur_retries = 0
    while event_keep_serial_active.is_set():
        line = readline_from_serial(ser)

        if "Retry count exceeded" in line:  # TFTP boot failed
            # https://github.com/u-boot/u-boot/blob/8c39999acb726ef083d3d5de12f20318ee0e5070/net/tftp.c#L704
            logging.warning(f"Failed booting from TFTP (attempt #{cur_retries}): {line}")
            cur_retries = cur_retries + 1
            if cur_retries > max_retries:
                write_to_serial(ser, b'\x03')
                raise RuntimeError(f"Maximum TFTP retries {max_retries} reached. Aborting")

        elif "Wrong Image Format for bootm command" in line:
            # https://github.com/u-boot/u-boot/blob/8c39999acb726ef083d3d5de12f20318ee0e5070/boot/bootm.c#L974
            logging.error("TFTP boot found wrong image format")

        elif "ERROR: can't get kernel image!" in line:
            # https://github.com/u-boot/u-boot/blob/8c39999acb726ef083d3d5de12f20318ee0e5070/boot/bootm.c#L123
            logging.error(f"Unable to boot initramfs file. Check you provided the correct file. Aborting.")
            import os
            os._exit(1)

        elif "## Booting kernel from FIT Image at" in line:  # with U-Boot v2009.x
            # https://github.com/u-boot/u-boot/blob/f20393c5e787b3776c179d20f82a86bda124d651/common/cmd_bootm.c#L897
            break

        # TODO: check if this works! the original check above might be called different with newer version of U-Boot:
        elif "## Loading kernel from FIT Image at" in line:  # with U-Boot v2013.07 and newer
            # https://github.com/u-boot/u-boot/blob/8c39999acb726ef083d3d5de12f20318ee0e5070/boot/image-fit.c#L2079
            break

        time.sleep(0.01)


def boot_wait_for_brlan(ser: serial.Serial):
    # The "eth0: Link is Up" comes up, then goes down again and then comes up again
    # It seems that the second "up" happens after eth0 has entered promiscuous mode
    # and after the bridge has been created

    while event_keep_serial_active.is_set():
        line = readline_from_serial(ser)

        if "br-lan: link becomes ready" in line:
            logging.info("br-lan is ready.")
            time.sleep(2)  # sometimes br-lan is ready but the default IP is still not reachable
            break

        time.sleep(0.1)


def boot_set_ips(ser, new_ap_ip):
    logging.info(f"Setting new AP ip to {new_ap_ip}")
    ip_str = new_ap_ip.with_prefixlen.encode('ascii')

    write_to_serial(ser, b'\n')  # login
    write_to_serial(ser, b'ip address del 192.168.1.1 dev br-lan\n')  # remove default IP to avoid collisions
    write_to_serial(ser, b'ip address add ' + ip_str + b' dev br-lan\n')
    write_to_serial(ser, b'ip -4 address show\n')
    time.sleep(0.5)


def keep_logging_until_reboot(ser: serial.Serial):
    while event_keep_serial_active.is_set():
        if ser.in_waiting > 5:
            line = readline_from_serial(ser)
            if "Upgrade completed" in line:
                logging.info("Flashing successful.")
            elif "reboot: Restarting system" in line:
                logging.info("Reboot detected. Stopping serial connection.")
                break

        time.sleep(0.05)


def start_tftp_boot_via_serial(name: str,
                               tftp_ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface,
                               tftp_file: str,
                               new_ap_ip: ipaddress.IPv4Interface | ipaddress.IPv6Interface):
    with serial.Serial(port=name, baudrate=115200, timeout=30) as ser:
        logging.info(f"Starting to connect to serial port {ser.name}")
        event_keep_serial_active.set()

        bootup_interrupt(ser)
        bootup_login(ser)
        bootup_login_verification(ser)
        bootup_set_boot_openwrt(ser)
        boot_via_tftp(ser, tftp_ip, tftp_file, new_ap_ip)
        boot_wait_for_brlan(ser)
        boot_set_ips(ser, new_ap_ip)
        event_ssh_ready.set()
        keep_logging_until_reboot(ser)


def write_to_serial(ser: serial.Serial, text: bytes, sleep: float = 0) -> str:
    ser.write(text)
    if sleep > 0:
        time.sleep(sleep)

    return_string = ser.readline().decode('ascii')
    debug_serial(return_string)
    return return_string


def readline_from_serial(ser: serial.Serial) -> str:
    bytestring = ser.readline()
    try:
        line = bytestring.decode('ascii')
    except UnicodeDecodeError:
        # We receive non-ascii/non-utf8 chars from the Linux kernel like 0xea or 0x90
        # after "Serial: 8250/16550 driver, 16 ports, IRQ sharing enabled"
        line = str(bytestring)
        line.replace(r'\n', '\n')

    debug_serial(line)
    return line


def start_tftp_server(tftp_dir: str, initramfs_filepath: str, ip: str = '0.0.0.0', port: int = 69) -> tftpy.TftpServer:
    import shutil
    import os.path
    shutil.copyfile(initramfs_filepath, os.path.join(tftp_dir, initramfs_filepath.name))

    logging.info(f"Starting tftp server on {ip}:{port} using {tftp_dir}")
    tftp_server = tftpy.TftpServer(tftp_dir)
    logging.debug(f"Files in ${tftp_dir}: {os.listdir(tftp_dir)}")
    tftp_thread = Thread(target=tftp_server.listen, args=[ip, port])
    tftp_thread.start()
    return tftp_server


def start_ssh(sysupgrade_firmware_path: str, ap_ip: str = '192.168.1.1'):
    import scp
    logging.info("SSH waiting for ready signal.")
    event_ssh_ready.wait()
    if event_abort_ssh.is_set():
        return
    logging.info("SSH Starting")
    paramiko.util.log_to_file('paramiko.log')
    with paramiko.Transport(ap_ip) as transport:
        transport.connect()  # ignoring all security
        transport.auth_none("root")  # password-less login

        firmware_target_path = '/tmp/firmware.bin'

        # Basic OpenWRT only supports SCP, not SFTP
        with scp.SCPClient(transport) as scp:
            scp.put(sysupgrade_firmware_path, firmware_target_path)

        with transport.open_session() as chan:
            sysupgrade_command = "sysupgrade -n " + firmware_target_path
            if DRYRUN:
                logging.info("dryrun: running sysupgrade with test and rebooting")
                sysupgrade_command = sysupgrade_command.replace('sysupgrade', 'sysupgrade --test')
                sysupgrade_command = sysupgrade_command + " && reboot"
            logging.debug(f"Running remote: {sysupgrade_command}")
            stdout = chan.makefile('r')
            stderr = chan.makefile_stderr('r')

            chan.exec_command(sysupgrade_command)
            sysupgrade_stdout = stdout.read().decode()
            sysupgrade_stderr = stderr.read().decode()
            logging.debug("sysupgrade stdout: " + sysupgrade_stdout)
            logging.debug("sysupgrade stderr: " + sysupgrade_stderr)

            # sysupgrade prints to stderr by default
            if "Commencing upgrade" in sysupgrade_stderr:
                logging.info("Flashing in progress...")
        logging.debug("Closing SSH session.")


def post_cleanup(tftp_server, ssh_thread, serial_thread):
    if ssh_thread and ssh_thread.is_alive():
        logging.info("Stopping SSH thread")
        event_abort_ssh.set()
        event_ssh_ready.set()
    if serial_thread and serial_thread.is_alive():
        logging.info("Stopping Serial thread")
        event_keep_serial_active.clear()

    logging.debug("Stopping TFTP server.")
    tftp_server.stop()  # set now=True to force shutdown


def main(serial_port: str, initramfs_path: pathlib.Path, sysupgrade_path: pathlib.Path, local_ip: str,
         ap_ip: str = None):
    tmpdir = tempfile.TemporaryDirectory()  # automatically cleaned up after process termination
    if not serial_port:
        serial_port = find_serial_port()

    ap_ip_interface, local_ip_interface = setting_up_ips(local_ip, ap_ip)

    tftp_server = start_tftp_server(tmpdir.name, initramfs_path, ip=str(local_ip_interface.ip))
    serial_thread = None
    ssh_thread = None
    try:
        serial_thread = Thread(target=start_tftp_boot_via_serial,
                               args=[serial_port, local_ip_interface, initramfs_path.name, ap_ip_interface],
                               daemon=True)
        ssh_thread = Thread(target=start_ssh, args=[sysupgrade_path, str(ap_ip_interface.ip)])
        logging.debug("Starting serial thread")
        serial_thread.start()
        logging.debug("Starting ssh thread")
        ssh_thread.start()

        logging.debug("Waiting for ssh thread")
        # Strange workaround to allow ctrl+c or system stop events during a join()
        while ssh_thread.is_alive():
            ssh_thread.join(5)  # wait for SSH to conclude its actions

        logging.debug("Waiting for serial thread")
        # Strange workaround to allow ctrl+c or system stop events during a join()
        while serial_thread.is_alive():
            serial_thread.join(5)

        logging.info("All steps finished. Give the AP some time to reboot and then access it on http://192.168.1.1")
    except (KeyboardInterrupt, SystemExit, SystemError):
        logging.warning("Aborting main process")
    finally:
        post_cleanup(tftp_server, ssh_thread, serial_thread)


def setting_up_ips(local_ip: str, ap_ip_str: str = None):
    # IP management
    local_ip_interface = ipaddress.ip_interface(local_ip)
    local_ip_interface = ip_address_fix_prefix(local_ip_interface)
    # the ap shall have one less than the broadcast address re-using the same prefix
    ap_ip_interface = ipaddress.ip_interface(
        f"{local_ip_interface.network.broadcast_address - 1}/{local_ip_interface.network.prefixlen}")

    if ap_ip_str:
        ap_ip_interface = ipaddress.ip_interface(ap_ip_str)
        ap_ip_interface = ip_address_fix_prefix(ap_ip_interface)

    if local_ip_interface.ip == ap_ip_interface.ip:
        raise ValueError(
            f"Local IP {local_ip_interface.with_prefixlen} and AP IP {ap_ip_interface.with_prefixlen} are identical.")

    # TODO: Check that AP and Local ip can reach each other: ap_ip_str.network.overlaps(...) maybe?
    return ap_ip_interface, local_ip_interface


def ip_address_fix_prefix(ip_interface: ipaddress.IPv4Interface | ipaddress.IPv6Interface) \
        -> ipaddress.IPv4Interface | ipaddress.IPv6Interface:
    if ip_interface.network.prefixlen == ip_interface.network.max_prefixlen:  # probably forgot to specify network
        if type(ip_interface) is ipaddress.IPv4Interface:
            new_prefix = 24
        elif type(ip_interface) is ipaddress.IPv6Interface:
            new_prefix = 64
        else:
            raise ValueError(f'Invalid IP interface received {type(ip_interface)}')

        logging.warning(f"Received too small network prefix {ip_interface.network.prefixlen} for {ip_interface}. "
                        + f"Assuming {ip_interface.ip}/{new_prefix}.")
        ip_interface = ipaddress.ip_interface(f"{ip_interface.ip}/{new_prefix}")
    elif ip_interface.network.prefixlen == ip_interface.network.max_prefixlen - 1:  # we need at least two IPs+broadcast
        raise ValueError(f"Too small IP prefix for {ip_interface}. Requires space for at least two IPs + broadcast.")

    return ip_interface


def test_serial_port(potential_serial_port):
    serial.Serial(port=potential_serial_port, baudrate=115200, timeout=45)
    return potential_serial_port


def find_serial_port():
    common_serial_ports = [
        '/dev/ttyUSB1',
        '/dev/ttyUSB0',
        'COM4',
        'COM3',
        'COM2',
        'COM1'  # COM1 needs to be last as it usually always exists
    ]
    for potential_serial_port in common_serial_ports:
        try:
            test_serial_port(potential_serial_port)
            return potential_serial_port
        except serial.serialutil.SerialException as e:
            if (
                    'FileNotFoundError' in str(e)  # Windows
                    or 'No such file or directory' in str(e)  # Linux: [Errno 2] No such file or directory: '/dev/tty..'
            ):
                logging.debug(f"Failed to access {potential_serial_port}.")
                continue
            raise
    raise RuntimeError(f"No valid accessible port found in {common_serial_ports}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        prog='ExtremeFlash',
        description='This tool helps flashing Extreme Networks or Enterasys access points')

    parser_group_force = parser.add_mutually_exclusive_group()
    parser_group_force.add_argument('-d', '--dryrun', action='store_true',
                                    help='Skip all steps that would make persistent changes')
    # parser_group_force.add_argument('-f', '--force', action='store_true',
    #                                 help='Ignore any safeguards. WARNING: This can be destructive.')

    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debugging output')
    parser.add_argument('-p', '--port', action='store', type=str,
                        help='The serial port to use to communicate with the access point', required=False)
    parser.add_argument('-i', '--initramfs', action='store', type=pathlib.Path,
                        help='The path to the initramfs for the access point', required=True)
    parser.add_argument('-j', '--image', action='store', type=pathlib.Path,
                        help='The path to the image that should be flashed on the access point', required=True)
    parser.add_argument('--local-ip', action='store', type=ipaddress.ip_interface,
                        help='The IP of a local interface that will run TFTP and communicate with the access point',
                        required=True)
    parser.add_argument('--ap-ip', action='store', type=ipaddress.ip_interface,
                        help='The (temporary) IP of the access point to communicate with. Defaults to broadcast ip-1.',
                        required=False)

    args = parser.parse_args()

    loglevel = logging.INFO
    if args.verbose:
        loglevel = logging.DEBUG

    logging.basicConfig(level=loglevel)
    logging.getLogger('tftpy').setLevel(logging.WARN if logging.WARN > loglevel else loglevel)  # tftpy is very spammy
    logging.getLogger('paramiko.transport').setLevel(logging.INFO if logging.INFO > loglevel else loglevel)

    if args.dryrun:
        DRYRUN = True

    main(args.port, args.initramfs, args.image, args.local_ip, args.ap_ip)
