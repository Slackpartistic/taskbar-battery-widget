"""Unit tests for the Compx 2.4 GHz keyboard receiver battery reader (fake handles only)."""
import threading
import time
from types import SimpleNamespace

import pytest

from taskbar_battery_widget import compx, hid_win, wireless

REQ_HEX = "0804000000000000000000000000000049000000"


def make_reply(pct: int, flag: int = 0, mv: int = 0x0FAD, extra: int = 0x02) -> bytes:
    b = bytearray(20)
    b[0], b[1], b[5], b[6], b[7] = 0x08, 0x04, extra, pct & 0xFF, flag
    b[8], b[9] = mv >> 8, mv & 0xFF
    b[16] = (0x55 - sum(b[:16])) & 0xFF
    return bytes(b)


def f75_info(path="hid#vid_3554&pid_fa09&mi_01&col01#8&1&0&0000", product="2.4G Wireless Receiver",
             vid=0x3554, pid=0xFA09, page=0xFF02, usage=0x0002, in_len=20, out_len=20):
    return SimpleNamespace(vid=vid, pid=pid, path=path, usage_page=page, usage=usage,
                           in_len=in_len, out_len=out_len, feat_len=0, product=product)


class FakeHandle:
    """Scripted handle: each write pops one item (bytes, list[bytes], or None) from `script`."""

    def __init__(self, script):
        self.script = list(script)
        self.queue: list[bytes] = []
        self.sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def drain(self):
        self.queue.clear()

    def write(self, data, timeout_ms=500):
        self.sent.append(bytes(data))
        if self.script:
            nxt = self.script.pop(0)
            if isinstance(nxt, list):
                self.queue.extend(nxt)
            elif nxt is not None:
                self.queue.append(nxt)
        return True

    def read(self, timeout_ms=0):
        return self.queue.pop(0) if self.queue else None


def run(script, infos=None, budget=2.0):
    h = FakeHandle(script)
    out = compx.read(infos if infos is not None else [f75_info()], time.monotonic() + budget, None,
                     handle_factory=lambda path, in_len=0: h)
    return out, h


def test_vendor_ids_and_pid_narrowing():
    assert compx.VENDOR_IDS == (0x3554,)
    assert 0x3554 in wireless.EXTRA_VENDOR_IDS
    assert hid_win._path_matches_vendor(r"\\?\hid#vid_3554&pid_fa09&mi_01&col01#x", (0x3554,))
    assert not hid_win._path_matches_vendor(r"\\?\hid#vid_3554&pid_f58a&mi_01&col01#x", (0x3554,))


def test_request_bytes_and_checksum():
    req = compx.build_request()
    assert req.hex() == REQ_HEX
    assert len(req) == 20 and sum(req[:17]) & 0xFF == 0x55


def test_documented_reply_parses():
    # 08 04 00 00 00 02 50 00 0f ad .. 3b, as published for this receiver (80 %, 4013 mV)
    b = bytearray(20)
    b[:10] = bytes.fromhex("08040000000250000fad")
    b[16] = 0x3B
    assert compx.frame_ok(bytes(b))
    assert compx.parse_power_reply(bytes(b)) == ("ok", 80, False)


def test_reads_battery_and_charging():
    out, h = run([make_reply(64)])
    d, = out
    assert h.sent == [bytes.fromhex(REQ_HEX)]
    assert (d.id, d.kind, d.battery, d.charging, d.connected, d.link, d.source) == \
        ("compx:fa09", "keyboard", 64, False, True, "2.4ghz", "hid")
    assert d.name == "2.4 GHz keyboard"
    out, _ = run([make_reply(90, flag=1)])
    assert out[0].charging is True and out[0].battery == 90


def test_zero_percent_is_unknown_not_a_reading():
    out, h = run([make_reply(0)])
    assert out[0].battery is None and out[0].connected and len(h.sent) == 1


@pytest.mark.parametrize("bad", [
    None,
    b"\x08\x04",                                        # short
    bytes(20),                                          # all zero
    make_reply(50)[:16] + b"\x00" + make_reply(50)[17:],   # checksum broken
    bytes([0x08, 0x05]) + make_reply(50)[2:],           # other command
    bytes([0x13]) + make_reply(50)[1:],                 # LED report id
])
def test_unrelated_or_bad_replies_are_ignored(bad):
    assert compx.parse_power_reply(bad) is None


def test_over_100_is_rejected():
    assert compx.parse_power_reply(make_reply(101)) is None


def test_skips_unrelated_reports_before_the_reply():
    noise = bytes([0x05, 0xE9, 0x00]) + bytes(17)       # consumer-control style report
    out, _ = run([[noise, noise, make_reply(42)]])
    assert out[0].battery == 42


def test_no_reply_yields_no_device_and_retries():
    out, h = run([None, None, None], budget=3.0)
    assert out == [] and len(h.sent) == compx.MAX_ATTEMPTS


def test_whitelist():
    with pytest.raises(ValueError):
        compx.build_request(0x05)
    w = compx._ReadOnly(FakeHandle([]))
    with pytest.raises(ValueError):
        w.write(bytes.fromhex("1300") + bytes(18))
    assert w.write(compx.build_request())


def test_deadline_and_cancel_before_write():
    h = FakeHandle([make_reply(50)])
    assert compx.read([f75_info()], time.monotonic() - 1, None, handle_factory=lambda p, in_len=0: h) == []
    ev = threading.Event()
    ev.set()
    assert compx.read([f75_info()], time.monotonic() + 1, ev, handle_factory=lambda p, in_len=0: h) == []
    assert h.sent == []


@pytest.mark.parametrize("kw", [
    {"pid": 0xF58A},                    # Compx mouse receiver: other protocol family
    {"vid": 0x258A},                    # the F75 on its cable
    {"page": 0x000C, "usage": 1, "in_len": 3, "out_len": 0},   # consumer collection
    {"page": 0xFF04, "in_len": 0, "out_len": 0},               # feature-only vendor collection
    {"usage": 0x0001},
    {"in_len": 64, "out_len": 64},
    {"path": "hid#{00001124-0000-1000-8000-00805f9b34fb}_vid&00023554_pid&fa09#x"},  # Bluetooth
])
def test_collection_selection(kw):
    out, h = run([make_reply(50)], infos=[f75_info(**kw)])
    assert out == [] and h.sent == []


def test_product_name_kept_when_specific():
    assert compx._display_name("AULA F75") == "AULA F75"
    assert compx._display_name("2.4G Wireless Receiver") == compx.DEFAULT_NAME


def test_two_receivers_get_distinct_ids():
    infos = [f75_info(path="hid#a"), f75_info(path="hid#b")]
    out, _ = run([make_reply(50), make_reply(60)], infos=infos)
    assert len(out) == 2 and out[0].id == "compx:fa09" and out[1].id.startswith("compx:fa09:")


def test_never_raises():
    def boom(path, in_len=0):
        raise OSError("x")
    assert compx.read([f75_info()], time.monotonic() + 1, None, handle_factory=boom) == []
    assert compx.read(None, time.monotonic() + 1) == []
