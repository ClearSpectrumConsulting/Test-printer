"""Tests against a simulated printer (no real hardware needed).

    python3 -m unittest -v
"""

import hashlib
import json
import os
import socket
import tempfile
import threading
import unittest

import epson_et2810_reset as tool

SERIAL = "X8GZ123456"
PREFIX = [int(p) for p in tool.EPSON_CTRL_OID.split(".")]


def decode_oid(payload):
    parts, value = [payload[0] // 40, payload[0] % 40], 0
    for byte in payload[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(value)
            value = 0
    return parts


class FakePrinter(threading.Thread):
    """UDP server that decodes SNMP GETs independently of the tool's encoder."""

    def __init__(self, model="EPSON ET-2810 Series", discard_writes=False):
        super().__init__(daemon=True)
        self.model = model
        self.discard_writes = discard_writes
        self.eeprom = {a: 0 for a in range(2048)}
        self.eeprom.update({48: 0x9A, 49: 0x17, 47: 0x00, 50: 0x10, 51: 0x0D,
                            54: 94, 55: 94, 255: 94, 0x100: 0x37})
        for i, ch in enumerate(SERIAL):
            self.eeprom[1604 + i] = ord(ch)
        self.temp_resets = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                return
            _, msg, _ = tool._read_tlv(data, 0)
            _, _, pos = tool._read_tlv(msg, 0)
            _, community, pos = tool._read_tlv(msg, pos)
            _, pdu, _ = tool._read_tlv(msg, pos)
            _, rid, pos = tool._read_tlv(pdu, 0)
            _, _, pos = tool._read_tlv(pdu, pos)
            _, _, pos = tool._read_tlv(pdu, pos)
            _, binds, _ = tool._read_tlv(pdu, pos)
            _, bind, _ = tool._read_tlv(binds, 0)
            _, oid_raw, _ = tool._read_tlv(bind, 0)
            assert community == b"public"
            value = self.answer(decode_oid(oid_raw))
            varbind = tool._tlv(0x30, tool._tlv(0x06, oid_raw)
                                + tool._tlv(0x04, value))
            reply = tool._tlv(0x30, tool._ber_int(0) + tool._tlv(0x04, community)
                              + tool._tlv(0xA2, tool._tlv(0x02, rid)
                                          + tool._ber_int(0) + tool._ber_int(0)
                                          + tool._tlv(0x30, varbind)))
            self.sock.sendto(reply, addr)

    def answer(self, oid):
        if ".".join(map(str, oid)) == tool.MODEL_OID:
            return self.model.encode()
        assert oid[:len(PREFIX)] == PREFIX, oid
        cmd = bytes(oid[len(PREFIX):])
        name, length, body = cmd[:2], cmd[2] | cmd[3] << 8, cmd[4:]
        assert length == len(body)
        if name == b"||":
            if tuple(body[:2]) != (74, 54):
                return b"\x00@BDC PS\r\n||:41:NA;\x0c"
            address = body[5] | body[6] << 8
            if bytes(body[2:5]) == bytes([65, 190, 160]):
                return b"\x00@BDC PS\r\nEE:%04X%02X;\x0c" % (
                    address, self.eeprom[address])
            if bytes(body[2:5]) == bytes([66, 189, 33]):
                if bytes(b - 1 for b in body[8:]) != b"Maribaya":
                    return b"\x00@BDC PS\r\n||:42:NA;\x0c"
                if not self.discard_writes:
                    self.eeprom[address] = body[7]
                return b"\x00@BDC PS\r\n||:42:OK;\x0c"
        if name == b"rw":
            if body == b"\x01\x00" + hashlib.sha1(SERIAL.encode()).digest():
                self.temp_resets += 1
                return b"\x00@BDC PS\r\nrw:01:OK;\x0c"
            return b"\x00@BDC PS\r\nrw:01:NA;\x0c"
        raise AssertionError("unexpected command %r" % cmd)


class ToolTest(unittest.TestCase):
    def start(self, **kwargs):
        fake = FakePrinter(**kwargs)
        fake.start()
        self.addCleanup(fake.sock.close)
        return fake

    def run_tool(self, fake, *args):
        command, rest = args[0], list(args[1:])
        return tool.main([command, "127.0.0.1", "--port", str(fake.port),
                          "--timeout", "1"] + rest)

    def test_known_oid_encoding(self):
        # Read of address 48, as documented by epson_print_conf.
        oid = (tool.EPSON_CTRL_OID + ".124.124.7.0.74.54.65.190.160.48.0")
        self.assertEqual(decode_oid(tool._ber_oid(oid)[2:]),
                         [int(p) for p in oid.split(".")])

    def test_levels(self):
        fake = self.start()
        printer = tool.Printer("127.0.0.1", fake.port, timeout=1)
        levels = printer.waste_levels()
        self.assertAlmostEqual(levels["Main pad"], 0x179A / 63.46)
        self.assertAlmostEqual(levels["Borderless pad"], 0x0D10 / 34.16)
        self.assertEqual(printer.serial(), SERIAL)

    def test_status_changes_nothing(self):
        fake = self.start()
        before = dict(fake.eeprom)
        self.assertEqual(self.run_tool(fake, "status"), 0)
        self.assertEqual(fake.eeprom, before)

    def test_reset_and_restore(self):
        fake = self.start()
        before = dict(fake.eeprom)
        backup = os.path.join(tempfile.mkdtemp(), "b.json")
        self.assertEqual(
            self.run_tool(fake, "reset", "--yes", "--backup", backup), 0)
        for address, value in tool.RESET_VALUES.items():
            self.assertEqual(fake.eeprom[address], value)
        self.assertEqual(fake.eeprom[0x100], 0x36)  # only bit 0 cleared
        touched = set(tool.RESET_VALUES) | {0x100}
        for address in before:
            if address not in touched:
                self.assertEqual(fake.eeprom[address], before[address])
        with open(backup) as fh:
            self.assertEqual(json.load(fh)["cells"]["48"], 0x9A)

        self.assertEqual(self.run_tool(fake, "restore", backup, "--yes"), 0)
        self.assertEqual(fake.eeprom, before)

    def test_wrong_model_refused(self):
        fake = self.start(model="EPSON XP-205 Series")
        before = dict(fake.eeprom)
        backup = os.path.join(tempfile.mkdtemp(), "b.json")
        self.assertEqual(
            self.run_tool(fake, "reset", "--yes", "--backup", backup), 1)
        self.assertEqual(fake.eeprom, before)

    def test_discarded_write_is_reported(self):
        fake = self.start(discard_writes=True)
        backup = os.path.join(tempfile.mkdtemp(), "b.json")
        self.assertEqual(
            self.run_tool(fake, "reset", "--yes", "--backup", backup), 1)

    def test_temp_reset(self):
        fake = self.start()
        self.assertEqual(self.run_tool(fake, "temp-reset"), 0)
        self.assertEqual(fake.temp_resets, 1)
        self.assertEqual(self.run_tool(fake, "temp-reset", "--serial", "WRONG"), 1)

    def test_no_printer(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        printer = tool.Printer("127.0.0.1", sock.getsockname()[1],
                               timeout=0.2, retries=1)
        with self.assertRaises(tool.PrinterError):
            printer.model()


if __name__ == "__main__":
    unittest.main()
