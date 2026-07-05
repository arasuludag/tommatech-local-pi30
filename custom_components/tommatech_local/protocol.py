"""EyBond transport + Voltronic PI30 (Q-protocol) framing.

Validated live against the collector dongle (PN W0824353291671) at
10.0.0.34: UDP 58899 `set>server=` redirect -> dongle dials back on TCP
8899 -> EyBond heartbeat (FC=1) -> Forward2Device (FC=4) wrapping classic
`QPIGS`/`QPIRI`/`QMOD` commands -> raw `(...` responses. Device reports
devcode 0x0102 in its heartbeat; the value placed in the FC=4 header does
not affect relaying, so 0x0994 is used. devaddr 0xFF and 0x01 both work.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone

HEADER = 8
UDP_DISCOVERY_PORT = 58899
TCP_DATA_PORT = 8899

# --- CRC-16/XMODEM (Voltronic command + response checksum) -----------
def _xmodem_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
        table.append(crc)
    return table


_XMODEM = _xmodem_table()


def crc16_xmodem(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFF) ^ _XMODEM[((crc >> 8) ^ b) & 0xFF]
    return crc


# Framing bytes that must not appear inside a CRC (they'd break frame parsing).
_STUFF = {0x28, 0x0D, 0x0A}  # '(', CR, LF


def _stuff(b: int) -> int:
    return (b + 1) & 0xFF if b in _STUFF else b


def build_q_command(cmd: str) -> bytes:
    """Classic Voltronic frame: <cmd><crc_hi><crc_lo><CR>."""
    body = cmd.encode("ascii")
    crc = crc16_xmodem(body)
    return body + bytes([_stuff((crc >> 8) & 0xFF), _stuff(crc & 0xFF), 0x0D])


# --- EyBond transport (8-byte big-endian header) ---------------------
def _header(tid: int, devcode: int, total_len: int, devaddr: int, fcode: int) -> bytes:
    # wire length field = total frame length - 6
    return struct.pack(">HHHBB", tid, devcode, total_len - 6, devaddr, fcode)


def build_heartbeat(tid: int, interval: int = 60) -> bytes:
    """FC=1 heartbeat. Payload = [Y-2000,M,D,H,Mi,S] + interval(2)."""
    now = datetime.now(timezone.utc)
    payload = bytes(
        [(now.year - 2000) & 0xFF, now.month, now.day, now.hour, now.minute, now.second]
    ) + struct.pack(">H", interval)
    return _header(tid, 0, HEADER + len(payload), 1, 1) + payload


def build_forward(tid: int, q_frame: bytes, devcode: int = 0x0994, devaddr: int = 1) -> bytes:
    """FC=4 Forward2Device wrapping a raw Voltronic command frame."""
    return _header(tid, devcode, HEADER + len(q_frame), devaddr, 4) + q_frame


def decode_header(data: bytes) -> tuple[int, int, int, int, int]:
    """-> (tid, devcode, total_len, devaddr, fcode)."""
    tid, devcode, wire_len, devaddr, fcode = struct.unpack(">HHHBB", data[:HEADER])
    return tid, devcode, wire_len + 6, devaddr, fcode


def parse_q_response(payload: bytes) -> str | None:
    """Strip '(' prefix + 2 CRC bytes + optional CR. Return the data string.

    Returns None for an empty/NAK payload so the caller can distinguish a
    real reading from a rejected command.
    """
    if not payload or payload[0] != 0x28:  # must start with '('
        return None
    end = -3 if payload and payload[-1] == 0x0D else -2
    text = payload[1:end].decode("ascii", errors="replace").strip()
    if text in ("", "NAK"):
        return None
    return text
