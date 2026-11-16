#!/usr/bin/env python3
"""
Waste ink pad counter tool for the Epson EcoTank ET-2810 family
(ET-2810 / 2811 / 2812 / 2813 / 2814 / 2815 / 2816 / 2818).

Talks to the printer over the network (SNMPv1, UDP port 161) using Epson's
"EPSON-CTRL" remote commands. No third-party packages are needed.

    python3 epson_et2810_reset.py status     192.168.1.50
    python3 epson_et2810_reset.py reset      192.168.1.50
    python3 epson_et2810_reset.py temp-reset 192.168.1.50
    python3 epson_et2810_reset.py restore    192.168.1.50 backup.json

EEPROM addresses and keys come from the open-source epson_print_conf project
(https://github.com/Ircama/epson_print_conf), entry "ET-2812" and its aliases.
"""

import argparse
import hashlib
import json
import random
import re
import socket
import struct
import sys
import time

# --- Printer data (ET-2812 family, which includes the ET-2810) -------------

SUPPORTED_MODELS = ("ET-2810", "ET-2811", "ET-2812", "ET-2813", "ET-2814",
                    "ET-2815", "ET-2816", "ET-2818")
READ_KEY = (74, 54)
WRITE_KEY = b"Maribaya"

# name -> (EEPROM addresses, least significant byte first; divider to get %)
WASTE_COUNTERS = {
    "Main pad":       ((48, 49, 47), 63.46),
    "Borderless pad": ((50, 51, 47), 34.16),
    "Third counter":  ((252, 253, 254), 13.0),
}
# Cells written by a reset, in order. The 94s are the "maintenance required"
# thresholds, rewritten to their factory value.
RESET_VALUES = {
    48: 0, 49: 0, 47: 0, 52: 0, 53: 0, 54: 94, 50: 0, 51: 0,
    55: 94, 28: 0, 252: 0, 253: 0, 254: 0, 255: 94,
}
# After the counters are zeroed, bit 0 of this cell (the latched "pad full"
# flag) is cleared; the other bits are left alone.
COMMIT_MASKS = {0x100: 0xFE}
SERIAL_ADDRESSES = range(1604, 1614)

EPSON_CTRL_OID = "1.3.6.1.4.1.1248.1.2.2.44.1.1.2.1"
MODEL_OID = "1.3.6.1.2.1.25.3.2.1.3.1"  # hrDeviceDescr


class PrinterError(Exception):
    pass


# --- Minimal SNMPv1 GET -----------------------------------------------------

def _ber_len(n):
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag, payload):
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _ber_int(n):
    return _tlv(0x02, n.to_bytes(n.bit_length() // 8 + 1, "big"))


def _ber_oid(oid):
    parts = [int(p) for p in oid.split(".")]
    out = bytearray([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, bytes(out))


def _read_tlv(data, pos):
    """Return (tag, payload, next_position)."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    if pos + length > len(data):
        raise PrinterError("truncated SNMP reply")
    return tag, data[pos:pos + length], pos + length


def build_get(oid, request_id, community=b"public"):
    varbind = _tlv(0x30, _ber_oid(oid) + _tlv(0x05, b""))
    pdu = _tlv(0xA0, _ber_int(request_id) + _ber_int(0) + _ber_int(0)
               + _tlv(0x30, varbind))
    return _tlv(0x30, _ber_int(0) + _tlv(0x04, community) + pdu)


def parse_response(packet):
    """Return (request_id, error_status, value_bytes or None)."""
    try:
        _, msg, _ = _read_tlv(packet, 0)
        _, _, pos = _read_tlv(msg, 0)            # version
        _, _, pos = _read_tlv(msg, pos)          # community
        tag, pdu, _ = _read_tlv(msg, pos)
        if tag != 0xA2:
            raise PrinterError("unexpected SNMP PDU type 0x%02x" % tag)
        _, rid, pos = _read_tlv(pdu, 0)
        _, err, pos = _read_tlv(pdu, pos)
        _, _, pos = _read_tlv(pdu, pos)          # error index
        _, binds, _ = _read_tlv(pdu, pos)
        _, bind, _ = _read_tlv(binds, 0)
        _, _, pos = _read_tlv(bind, 0)           # oid
        vtag, value, _ = _read_tlv(bind, pos)
    except IndexError:
        raise PrinterError("malformed SNMP reply")
    return (int.from_bytes(rid, "big"), int.from_bytes(err, "big"),
            value if vtag == 0x04 else None)


class Printer:
    def __init__(self, host, port=161, timeout=3.0, retries=3):
        self.addr = (host, port)
        self.timeout = timeout
        self.retries = retries
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def get(self, oid):
        """SNMP GET; returns the OCTET STRING value (bytes) or None."""
        rid = random.randint(1, 0x7FFFFFFF)
        packet = build_get(oid, rid)
        for _ in range(self.retries):
            self.sock.sendto(packet, self.addr)
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.sock.settimeout(remaining)
                try:
                    data, _ = self.sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError as exc:
                    raise PrinterError("network error: %s" % exc)
                got_rid, err, value = parse_response(data)
                if got_rid != rid:
                    continue  # stale reply to an earlier attempt
                return None if err else value
        raise PrinterError(
            "no answer from %s:%d - check the IP address and that the printer "
            "is on, awake and on the same network" % self.addr)

    # --- Epson remote commands ---

    def ctrl(self, command, payload):
        """Send a two-letter EPSON-CTRL command, return the raw reply."""
        cmd = command.encode() + struct.pack("<H", len(payload)) + bytes(payload)
        return self.get(EPSON_CTRL_OID + "." + ".".join(str(b) for b in cmd))

    def model(self):
        value = self.get(MODEL_OID)
        return value.decode("latin-1").strip() if value else ""

    def read(self, address):
        reply = self.ctrl("||", [READ_KEY[0], READ_KEY[1], 65, 190, 160,
                                 address & 0xFF, address >> 8])
        match = re.search(rb"EE:([0-9A-Fa-f]{4})([0-9A-Fa-f]{2})", reply or b"")
        if not match:
            raise PrinterError(
                "printer refused to read EEPROM address %d (reply: %r)"
                % (address, reply))
        if int(match.group(1), 16) != address:
            raise PrinterError("EEPROM address mismatch reading %d" % address)
        return int(match.group(2), 16)

    def write(self, address, value):
        """Write one byte and confirm it by reading it back."""
        key = [0 if b == 0 else b + 1 for b in WRITE_KEY]
        reply = self.ctrl("||", [READ_KEY[0], READ_KEY[1], 66, 189, 33,
                                 address & 0xFF, address >> 8, value] + key)
        if not reply or b":OK;" not in reply:
            raise PrinterError(
                "printer refused to write EEPROM address %d (reply: %r)"
                % (address, reply))
        actual = self.read(address)
        if actual != value:
            raise PrinterError(
                "address %d still reads %d after writing %d - this firmware "
                "discards EEPROM writes" % (address, actual, value))

    def serial(self):
        return "".join(chr(self.read(a)) for a in SERIAL_ADDRESSES)

    def waste_levels(self):
        levels = {}
        for name, (addresses, divider) in WASTE_COUNTERS.items():
            raw = sum(self.read(a) << (8 * i) for i, a in enumerate(addresses))
            levels[name] = raw / divider
        return levels

    def temp_reset(self, serial):
        reply = self.ctrl("rw", struct.pack("<H", 1)
                          + hashlib.sha1(serial.encode()).digest())
        return bool(reply) and b"rw:" in reply and b":OK;" in reply


# --- Commands ---------------------------------------------------------------

def check_model(printer, force):
    model = printer.model()
    print("Printer model : %s" % (model or "(not reported)"))
    if any(m in model for m in SUPPORTED_MODELS):
        return
    if force:
        print("WARNING: not a recognised ET-2810 family model; continuing "
              "because --force was given.")
        return
    raise PrinterError(
        "this does not look like an ET-2810 family printer. EEPROM addresses "
        "differ between models, so writing could damage its settings. "
        "Use --force only if you are sure.")


def print_levels(printer):
    for name, percent in printer.waste_levels().items():
        print("  %-15s %6.1f %%" % (name, percent))


def confirm(args, prompt):
    if args.yes:
        return
    if input(prompt + " Type RESET to continue: ").strip() != "RESET":
        raise PrinterError("cancelled, nothing was changed")


def cmd_status(printer, args):
    check_model(printer, force=True)
    print("Serial number : %s" % printer.serial())
    print("Waste ink counters:")
    print_levels(printer)
    flag = printer.read(0x100)
    print("Pad-full flag : %s" % ("SET" if flag & 1 else "clear"))


def cmd_reset(printer, args):
    check_model(printer, args.force)
    print("Current waste ink counters:")
    print_levels(printer)

    addresses = list(RESET_VALUES) + list(COMMIT_MASKS)
    backup = {str(a): printer.read(a) for a in addresses}
    path = args.backup or "et2810-backup-%s.json" % time.strftime("%Y%m%d-%H%M%S")
    with open(path, "w") as fh:
        json.dump({"model": printer.model(), "cells": backup}, fh, indent=2)
    print("Backup of the %d cells to be changed saved to %s" % (len(backup), path))

    confirm(args, "This permanently rewrites the waste ink counters.")
    for address, value in RESET_VALUES.items():
        printer.write(address, value)
    for address, mask in COMMIT_MASKS.items():
        current = printer.read(address)
        if current & mask != current:
            printer.write(address, current & mask)

    print("Done. New waste ink counters:")
    print_levels(printer)
    print("Now switch the printer off and on again.")


def cmd_restore(printer, args):
    check_model(printer, args.force)
    with open(args.file) as fh:
        cells = {int(a): int(v) for a, v in json.load(fh)["cells"].items()}
    allowed = set(RESET_VALUES) | set(COMMIT_MASKS)
    if not cells or not set(cells) <= allowed or \
            not all(0 <= v <= 255 for v in cells.values()):
        raise PrinterError("%s is not a backup made by this tool" % args.file)
    confirm(args, "This writes %d backed-up cells to the printer." % len(cells))
    for address, value in cells.items():
        printer.write(address, value)
    print("Backup restored. Switch the printer off and on again.")


def cmd_temp_reset(printer, args):
    check_model(printer, args.force)
    serial = args.serial or printer.serial()
    print("Serial number : %s" % serial)
    if printer.temp_reset(serial):
        print("Printer accepted the temporary reset. Switch it off and on again.")
    else:
        raise PrinterError("printer did not accept the temporary reset")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read and reset the waste ink pad counters of an Epson "
                    "ET-2810 family printer over the network.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("host", help="printer IP address or hostname")
        p.add_argument("--port", type=int, default=161, help=argparse.SUPPRESS)
        p.add_argument("--timeout", type=float, default=3.0,
                       help="seconds to wait for each answer (default 3)")
        p.set_defaults(func=func)
        return p

    def add_writing(p):
        p.add_argument("--yes", action="store_true",
                       help="do not ask for confirmation")
        p.add_argument("--force", action="store_true",
                       help="continue even if the model is not recognised")

    add("status", cmd_status, "show the counters (changes nothing)")
    p = add("reset", cmd_reset, "back up, then zero the waste ink counters")
    p.add_argument("--backup", help="backup file name (default: timestamped)")
    add_writing(p)
    p = add("restore", cmd_restore, "write a backup file back to the printer")
    p.add_argument("file", help="backup file made by 'reset'")
    add_writing(p)
    p = add("temp-reset", cmd_temp_reset,
            "ask the firmware for a temporary reset (no EEPROM write)")
    p.add_argument("--serial", help="serial number from the printer's label, "
                                    "if it cannot be read automatically")
    p.add_argument("--force", action="store_true",
                   help="continue even if the model is not recognised")

    args = parser.parse_args(argv)
    try:
        args.func(Printer(args.host, args.port, args.timeout), args)
    except PrinterError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
