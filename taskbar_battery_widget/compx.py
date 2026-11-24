"""Direct battery reader for keyboards on a Compx 2.4 GHz receiver (``3554:FA09``).

The receiver ships with the AULA F75 (and other keyboards built on the same Compx radio) and
enumerates as product ``"2.4G Wireless Receiver"``. Its control collection is vendor usage page
``0xFF02``, usage ``0x0002``, with 20-byte input/output reports (report id included).

Protocol (17-byte frames, report id ``0x08``, padded with zeros to the 20-byte report):

* Request: ``08 04 00 .. 00 <sum>``. Command ``0x04`` asks for the power details; the last frame
  byte (index 16) makes the 8-bit sum of the 17 frame bytes equal ``0x55``.
* Reply: ``08 04 .. .. .. .. <pct> <flag> <mv hi> <mv lo> .. <sum>`` with the same checksum rule;
  ``pct`` (index 6) is the level 1..100, ``flag`` (index 7) is 1 while charging. ``0`` % means the
  keyboard is asleep or out of range: no fresh reading, so the cache keeps the last value.

Protocol facts come from public documentation of the Compx/G-Wolves 0x08 framing (HaloBattery's
notes on the AULA F75 receiver and G-Wolves' web driver) and a HID scan of a real F75 receiver;
this is an independent implementation and only the whitelisted ``0x04`` query can ever be sent.
"""
from __future__ import annotations

import logging
import threading
import time

from .hid_win import _is_bluetooth_path, path_hash
from .models import Device
from .naming import sanitize_name

log = logging.getLogger(__name__)

COMPX_VID = 0x3554
KEYBOARD_RECEIVER_PIDS = frozenset({0xFA09})
VENDOR_IDS: tuple[int, ...] = (COMPX_VID,)

CONTROL_USAGE_PAGE = 0xFF02
CONTROL_USAGE = 0x0002
REPORT_LEN = 20             # Win32 report length, report id included
FRAME_LEN = 17
REPORT_ID = 0x08
CHECKSUM_TARGET = 0x55

CMD_POWER = 0x04
ALLOWED_COMMANDS = frozenset({CMD_POWER})

DEFAULT_NAME = "2.4 GHz keyboard"
DEVICE_BUDGET_S = 1.2
ATTEMPT_WAIT_MS = 350
MAX_ATTEMPTS = 3
MAX_SKIPPED_REPORTS = 8


def frame_ok(frame: bytes) -> bool:
    """True when the 17 frame bytes sum to 0x55 (mod 256)."""
    return len(frame) >= FRAME_LEN and sum(frame[:FRAME_LEN]) & 0xFF == CHECKSUM_TARGET


def build_request(command: int = CMD_POWER) -> bytes:
    """20-byte Win32 output report carrying the 17-byte query frame."""
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"command 0x{command:02x} is not allowed")
    b = bytearray(REPORT_LEN)
    b[0] = REPORT_ID
    b[1] = command
    b[FRAME_LEN - 1] = (CHECKSUM_TARGET - sum(b[:FRAME_LEN - 1])) & 0xFF
    return bytes(b)


_ALLOWED_PACKETS = frozenset({build_request(c) for c in ALLOWED_COMMANDS})


def parse_power_reply(buf: bytes | None) -> tuple[str, int | None, bool] | None:
    """``('ok', pct, charging)`` | ``('unknown', None, charging)`` for 0 % | ``None`` if unrelated/bad."""
    if not buf or len(buf) < FRAME_LEN:
        return None
    b = bytes(buf)
    if b[0] != REPORT_ID or b[1] != CMD_POWER or not frame_ok(b):
        return None
    pct, charging = b[6], b[7] == 0x01
    if pct == 0:
        return "unknown", None, charging
    if pct > 100:
        return None
    return "ok", int(pct), charging


def _matches(info) -> bool:
    if getattr(info, "vid", 0) != COMPX_VID or getattr(info, "pid", 0) not in KEYBOARD_RECEIVER_PIDS:
        return False
    if _is_bluetooth_path(getattr(info, "path", "") or ""):
        return False
    if getattr(info, "usage_page", 0) != CONTROL_USAGE_PAGE or getattr(info, "usage", 0) != CONTROL_USAGE:
        return False
    return getattr(info, "in_len", 0) == REPORT_LEN and getattr(info, "out_len", 0) == REPORT_LEN


def _display_name(product: str) -> str:
    clean = sanitize_name(product or "")
    low = clean.lower()
    if not clean or "dongle" in low or "receiver" in low:
        return DEFAULT_NAME
    return clean


class _ReadOnly:
    """Whitelist wrapper: only the power query can ever be written."""

    def __init__(self, h):
        self._h = h

    def drain(self) -> None:
        self._h.drain()

    def read(self, timeout_ms: int = ATTEMPT_WAIT_MS) -> bytes | None:
        return self._h.read(timeout_ms=timeout_ms)

    def write(self, data: bytes, timeout_ms: int = 500) -> bool:
        raw = bytes(data)
        if raw not in _ALLOWED_PACKETS:
            raise ValueError("write is not allowed")
        return bool(self._h.write(raw, timeout_ms=timeout_ms))


def _expired(deadline: float, cancel: threading.Event | None) -> bool:
    return (cancel is not None and cancel.is_set()) or time.monotonic() >= deadline


def _query_once(h: _ReadOnly, deadline: float, cancel: threading.Event | None):
    if _expired(deadline, cancel):
        return None
    h.drain()
    left_ms = int((deadline - time.monotonic()) * 1000)
    if left_ms <= 0 or not h.write(build_request(CMD_POWER), timeout_ms=max(1, min(500, left_ms))):
        return None
    until = min(deadline, time.monotonic() + ATTEMPT_WAIT_MS / 1000.0)
    for _ in range(MAX_SKIPPED_REPORTS):
        if _expired(deadline, cancel):
            return None
        wait_ms = int((until - time.monotonic()) * 1000)
        if wait_ms <= 0:
            return None
        r = h.read(timeout_ms=max(1, min(ATTEMPT_WAIT_MS, wait_ms)))
        if r is None:
            return None
        parsed = parse_power_reply(r)
        if parsed is not None:
            return parsed
    return None


def _read_one(info, deadline: float, cancel: threading.Event | None, handle_factory):
    phash = path_hash(info.path)
    result = None
    with handle_factory(info.path, in_len=REPORT_LEN) as raw:
        h = _ReadOnly(raw)
        for _ in range(MAX_ATTEMPTS):
            if _expired(deadline, cancel):
                break
            res = _query_once(h, deadline, cancel)
            if res is not None:
                result = res
                break
    if result is None:
        return None
    state, pct, charging = result
    dev = Device(
        id=f"compx:{info.pid:04x}",
        name=_display_name(getattr(info, "product", "") or ""),
        kind="keyboard",
        battery=pct if state == "ok" else None,
        charging=charging,
        connected=True,
        link="2.4ghz",
        source="hid",
    )
    return phash, dev


def read(infos, deadline: float, cancel: threading.Event | None = None,
         handle_factory=None) -> list[Device]:
    """Read every supported Compx keyboard receiver in ``infos``. Never raises."""
    try:
        if handle_factory is None:
            from .hid_win import HidHandle
            handle_factory = HidHandle
        mine = [i for i in (infos or ()) if _matches(i)]
        done: set[str] = set()
        rank: dict[str, int] = {}
        out: list[Device] = []
        for i in sorted(mine, key=lambda x: x.path):
            if _expired(deadline, cancel):
                break
            phash = path_hash(i.path)
            if phash in done:
                continue
            try:
                res = _read_one(i, min(deadline, time.monotonic() + DEVICE_BUDGET_S),
                                cancel, handle_factory)
            except Exception:
                log.debug("compx read failed for %04x:%04x", i.vid, i.pid, exc_info=True)
                continue
            if res is None:
                continue
            done.add(phash)
            _, dev = res
            nth = rank.get(dev.id, 0)
            rank[dev.id] = nth + 1
            if nth:
                dev.id = f"{dev.id}:{phash}"
            out.append(dev)
        return out
    except Exception:
        log.debug("compx scan failed", exc_info=True)
        return []
