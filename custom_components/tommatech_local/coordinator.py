"""Coordinator: EyBond TCP server + UDP announcer + PI30 poll loop.

The collector dongle does not serve data; it dials out to whichever server
the UDP `set>server=` broadcast names. We announce ourselves, accept the
dongle's TCP connection, then drive it with heartbeats + Forward2Device
polls. Parsed values live in `self.data` and are pushed to entities via
dispatcher. The redirect is transient: if HA stops announcing, the dongle
falls back to its configured cloud server on its own.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DEFAULT_TCP_PORT, DEFAULT_UDP_PORT, DEVICE_STATUS_BITS, DOMAIN,
    POLL_INTERVALS, QPIGS2_FIELDS, QPIGS_FIELDS, QPIRI_FIELDS,
    QPIWS_BITS, QPIWS_INFORMATIONAL, QPIWS_SUPPRESSED, STARTUP_COMMANDS,
)
from .protocol import (
    HEADER, build_forward, build_heartbeat, build_q_command,
    decode_header, parse_q_response,
)

_LOGGER = logging.getLogger(__name__)

SIGNAL_UPDATE = f"{DOMAIN}_update"
REQUEST_TIMEOUT = 6.0
HEARTBEAT_EVERY = 50  # seconds; keeps the dongle owned by us
# Hold last values through a brief collector drop (WiFi hiccup / re-dial)
# instead of flipping every entity to unavailable. A genuine outage still
# surfaces as unavailable once this window passes.
AVAILABILITY_GRACE = 90  # seconds


def _parse_fields(raw: str, field_map: dict[str, int]) -> dict[str, float]:
    parts = raw.split()
    out: dict[str, float] = {}
    for key, idx in field_map.items():
        if idx < len(parts):
            try:
                out[key] = round(float(parts[idx]), 3)
            except ValueError:
                continue
    return out


class InverterCoordinator:
    """Owns the socket lifecycle and the current decoded state."""

    def __init__(self, hass: HomeAssistant, host: str, devaddr: int) -> None:
        self.hass = hass
        self.host = host
        self.devaddr = devaddr
        self.data: dict[str, Any] = {"connected": False, "pn": None}
        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._tid = 0
        self._tasks: list[asyncio.Task] = []
        self._last_poll: dict[str, float] = {}
        self._startup_done = False
        self._disconnected_at: float | None = None

    def entities_available(self) -> bool:
        """True while the link is up, or briefly after a drop so entities
        ride out a transient collector reconnect on their last values."""
        if self.data.get("connected"):
            return True
        if self._disconnected_at is None:
            return False
        return (time.monotonic() - self._disconnected_at) < AVAILABILITY_GRACE

    # -- lifecycle ----------------------------------------------------
    async def async_start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_conn, "0.0.0.0", DEFAULT_TCP_PORT
        )
        self._tasks.append(self.hass.loop.create_task(self._announcer()))
        _LOGGER.info(
            "Tommatech Local listening on :%d, announcing to %s",
            DEFAULT_TCP_PORT, self.host,
        )

    async def async_stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._writer:
            self._writer.close()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) & 0xFFFF
        return self._tid

    # -- UDP announcer: keep telling the dongle to connect to us ------
    async def _announcer(self) -> None:
        import socket

        loop = asyncio.get_running_loop()
        while True:
            if not self.data["connected"]:
                # Updates are push-only, so nudge entities to re-check
                # availability each round; once AVAILABILITY_GRACE passes
                # they flip from "last value" to unavailable on their own.
                async_dispatcher_send(self.hass, SIGNAL_UPDATE)
                # Resolve our IP fresh each round: at HA boot after a power
                # outage the network may not be up yet, and the address can
                # legitimately change between outages.
                ip = _local_ip(self.host)
                if ip == "0.0.0.0":
                    await asyncio.sleep(15)
                    continue
                msg = f"set>server={ip}:{DEFAULT_TCP_PORT};".encode()
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sock.setblocking(False)
                    for dest in (self.host, _broadcast_of(self.host)):
                        try:
                            await loop.sock_sendto(sock, msg, (dest, DEFAULT_UDP_PORT))
                        except OSError:
                            pass
                    sock.close()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("announcer error: %s", err)
            await asyncio.sleep(15)

    # -- TCP connection handling --------------------------------------
    async def _handle_conn(self, reader, writer) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("Collector connected from %s", peer)
        if self._writer:
            self._writer.close()
        self._writer = writer
        self.data["connected"] = True
        self._disconnected_at = None
        self._startup_done = False
        writer.write(build_heartbeat(self._next_tid(), HEARTBEAT_EVERY))
        await writer.drain()
        poll_task = self.hass.loop.create_task(self._poll_loop())
        hb_task = self.hass.loop.create_task(self._heartbeat_loop(writer))
        try:
            await self._read_loop(reader)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            poll_task.cancel()
            hb_task.cancel()
            self.data["connected"] = False
            self._disconnected_at = time.monotonic()
            if self._writer is writer:
                self._writer = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("collector disconnected"))
            self._pending.clear()
            async_dispatcher_send(self.hass, SIGNAL_UPDATE)
            _LOGGER.info("Collector disconnected")

    async def _heartbeat_loop(self, writer) -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_EVERY)
                writer.write(build_heartbeat(self._next_tid(), HEARTBEAT_EVERY))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass

    async def _read_loop(self, reader) -> None:
        while True:
            head = await reader.readexactly(HEADER)
            tid, devcode, total_len, devaddr, fcode = decode_header(head)
            payload = (
                await reader.readexactly(total_len - HEADER)
                if total_len > HEADER else b""
            )
            if fcode == 1:  # heartbeat response carries the PN serial
                pn = payload[:14].decode("ascii", "replace").strip("\x00")
                if pn:
                    self.data["pn"] = pn
            elif fcode == 4:  # forwarded inverter response
                fut = self._pending.pop(tid, None)
                if fut and not fut.done():
                    fut.set_result(payload)

    # -- polling ------------------------------------------------------
    async def _poll_loop(self) -> None:
        await asyncio.sleep(1)  # let the first heartbeat round-trip
        try:
            while True:
                if not self._startup_done:
                    for cmd in STARTUP_COMMANDS:
                        await self._poll_one(cmd)
                        await asyncio.sleep(0.3)
                    self._startup_done = True
                now = time.monotonic()
                due = [
                    cmd for cmd, interval in POLL_INTERVALS.items()
                    if now - self._last_poll.get(cmd, 0) >= interval
                ]
                for cmd in due:
                    await self._poll_one(cmd)
                    self._last_poll[cmd] = time.monotonic()
                    await asyncio.sleep(0.3)  # pace the RS485 bus
                if due:
                    async_dispatcher_send(self.hass, SIGNAL_UPDATE)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOGGER.exception("poll loop died")

    async def _poll_one(self, cmd: str) -> None:
        wire_cmd = cmd
        year = datetime.now().year
        if cmd == "QEY":
            wire_cmd = f"QEY{year}"
        elif cmd == "QLY":
            wire_cmd = f"QLY{year}"
        raw = await self._query(wire_cmd)
        if raw is None:
            return
        if cmd == "QPIGS":
            parsed = _parse_fields(raw, QPIGS_FIELDS)
            parts = raw.split()
            if len(parts) > 16:
                parsed["status_bits"] = _parse_status_bits(parts[16])
            self.data["GS"] = parsed
        elif cmd == "QPIGS2":
            self.data["GS2"] = _parse_fields(raw, QPIGS2_FIELDS)
        elif cmd == "QPIRI":
            self.data["PIRI"] = _parse_fields(raw, QPIRI_FIELDS)
        elif cmd == "QMOD":
            self.data["MODE"] = raw.strip()
        elif cmd == "QPIWS":
            self.data["WARN_RAW"] = raw.strip()
            self.data["WARNINGS"] = _decode_warnings(raw.strip())
        elif cmd == "QET":
            self.data["ET_KWH"] = _wh_to_kwh(raw)
        elif cmd == "QLT":
            self.data["LT_KWH"] = _wh_to_kwh(raw)
        elif cmd == "QEY":
            self.data["EY_KWH"] = _wh_to_kwh(raw)
        elif cmd == "QLY":
            self.data["LY_KWH"] = _wh_to_kwh(raw)
        elif cmd == "QID":
            self.data["serial"] = raw.strip()
        elif cmd == "QVFW":
            self.data["firmware"] = raw.strip().replace("VERFW:", "")
        elif cmd == "QGMN":
            self.data["model_code"] = raw.strip()

    async def _query(self, cmd: str) -> str | None:
        if not self._writer:
            return None
        tid = self._next_tid()
        fut: asyncio.Future = self.hass.loop.create_future()
        self._pending[tid] = fut
        try:
            self._writer.write(
                build_forward(tid, build_q_command(cmd), devaddr=self.devaddr)
            )
            await self._writer.drain()
            payload = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            self._pending.pop(tid, None)
            return None
        return parse_q_response(payload)

    # -- set commands (write) -----------------------------------------
    async def async_set_command(self, command: str) -> bool:
        """Send a raw Voltronic set command (e.g. 'PCVV57.6'). Returns ACK."""
        if not self._writer:
            raise ConnectionError("collector not connected")
        tid = self._next_tid()
        fut: asyncio.Future = self.hass.loop.create_future()
        self._pending[tid] = fut
        self._writer.write(
            build_forward(tid, build_q_command(command), devaddr=self.devaddr)
        )
        await self._writer.drain()
        try:
            payload = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except (asyncio.TimeoutError, ConnectionError, OSError):
            self._pending.pop(tid, None)
            return False
        resp = (payload or b"").decode("ascii", "replace")
        acked = "ACK" in resp
        _LOGGER.info("SET %s -> %s", command, "ACK" if acked else resp.strip())
        # refresh ratings promptly so entities reflect the new value
        self._last_poll["QPIRI"] = 0
        return acked


def _parse_status_bits(bit_str: str) -> dict[str, bool]:
    # bit_str is b7..b0 left to right
    out: dict[str, bool] = {}
    if len(bit_str) == 8 and set(bit_str) <= {"0", "1"}:
        for name, bit in DEVICE_STATUS_BITS.items():
            out[name] = bit_str[7 - bit] == "1"
    return out


def _decode_warnings(bits: str) -> list[str]:
    active = []
    for idx, name in QPIWS_BITS.items():
        if idx in QPIWS_SUPPRESSED:
            continue  # known false positive on this unit; see const.py
        if idx < len(bits) and bits[idx] == "1":
            active.append(name)
    return active


def warnings_excluding_informational(warnings: list[str]) -> list[str]:
    informational = {QPIWS_BITS[i] for i in QPIWS_INFORMATIONAL}
    return [w for w in warnings if w not in informational]


def _wh_to_kwh(raw: str) -> float | None:
    tok = raw.strip().split()[0] if raw.strip() else ""
    try:
        return round(int(tok) / 1000.0, 2)
    except ValueError:
        return None


def _local_ip(target: str) -> str:
    """Best-effort local source IP that can reach `target`."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 1))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def _broadcast_of(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:3] + ["255"]) if len(parts) == 4 else "255.255.255.255"
