"""Radio driver.

Two layers:

* :class:`RadioCore` — synchronous AT-protocol driver against a duck-typed
  pyserial.Serial.  No Qt.  Fully testable with the MockSerial fixture.
* :class:`Radio` — :class:`QObject` wrapper that owns a RadioCore on a worker
  thread.  All UI interaction is via signals/slots; queued connections move
  work onto the worker thread automatically.

The split exists so unit tests can exercise the protocol layer without a
QApplication, and so threading is a thin shell over a well-tested core.
"""
from __future__ import annotations

import concurrent.futures
import functools
import re
import threading
import time
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from . import multicomm
from . import protocol as proto
from .protocol import Ati5Result, RssiReport
from .registers import FAMILY_MULTICOMM, FAMILY_SIK, ParamMeta, ParamValue


class RadioError(RuntimeError):
    """Generic radio-protocol failure."""


_OK_LINE = re.compile(r"(?:^|[\r\n])\s*OK\s*(?:[\r\n]|$)")


def _has_ok(text: str) -> bool:
    """True if `text` contains an ``OK`` reply line.

    Matching "OK" anywhere (case-insensitively) let line noise or a
    telemetry stream at the wrong baud pass for a radio reply.
    """
    return bool(_OK_LINE.search(text))


class _SerialLike(Protocol):
    timeout: float | None
    is_open: bool
    in_waiting: int

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def flush(self) -> None: ...
    def reset_input_buffer(self) -> None: ...
    def close(self) -> None: ...


# --------------------------------------------------------------------- RadioCore
class RadioCore:
    """Synchronous AT-protocol driver.

    Methods raise :class:`RadioError` on protocol failure.  The caller is
    responsible for sequencing `enter_command_mode` / `exit_command_mode`
    around any block of AT operations — `Radio` does this automatically.

    The :attr:`lock` is reentrant (so methods can call each other) and is
    held for the duration of any I/O against the serial port.  Threads that
    only want to read passthrough bytes from the port (e.g. the GUI-thread
    read pump) should ``try_acquire(blocking=False)`` and skip the tick when
    the lock is held — that way a long +++ bracket can't have its AT/OK
    response stolen out of the input buffer by a concurrent reader.
    """

    # MultiComm ATI5 can be truncated by the radio's UART buffer (manual
    # §3.2), so registers are read in ranges of this many via ATI5:x:y.  The
    # array registers S45..S49 are ~100 chars each, hence the small chunk.
    MULTICOMM_CHUNK = 5

    def __init__(self, ser: _SerialLike) -> None:
        self._ser = ser
        self.lock = threading.RLock()
        # Firmware family: learned from the ATI banner (identify) or the
        # ATI5 parameter names (read_params).  Drives protocol differences.
        self.family: str = FAMILY_SIK
        # MultiComm only: NODEID that RT commands are addressed to
        # ("RTI5@2").  None broadcasts — fine for a pair, but in a network
        # every node would answer at once.
        self.remote_node: int | None = None
        # Highest sreg seen on the local radio; bounds remote MultiComm reads.
        self._max_sreg: int = multicomm.MAX_SREG
        # Whether registers past _max_sreg were already probed for.
        self._probed_beyond = False
        # MultiComm per-register ranges/options (ATI10:n), fetched once per
        # connection from the local radio and reused for the remote panel.
        self._meta_cache: dict[int, ParamMeta] = {}
        self._meta_fetched = False

    @property
    def serial(self) -> _SerialLike:
        return self._ser

    @property
    def is_multicomm(self) -> bool:
        return self.family == FAMILY_MULTICOMM

    def _node(self, remote: bool) -> int | None:
        """The @NODEID suffix target — MultiComm remote commands only.  SiK
        has a single remote and would reject the suffix."""
        return self.remote_node if remote and self.is_multicomm else None

    def _remote_timing(self, remote: bool, timeout: float) -> tuple[float, float]:
        """(timeout, idle_gap) for a command.  MultiComm remote replies come
        back over the TDM cycle (SLOTS × TOTALUS, ~80 ms default but several
        hundred ms in a network), so they arrive in bursts with gaps."""
        if remote and self.is_multicomm:
            return max(timeout, 3.0), 0.5
        return timeout, 0.15

    # ---------------------------------------- low-level I/O
    def _send_collect(
        self,
        cmd: bytes,
        *,
        timeout: float = 1.0,
        idle_gap: float = 0.15,
    ) -> str:
        """Send `cmd`, then read until either `timeout` elapses or the input
        has been silent for `idle_gap` seconds after at least one byte arrived.
        """
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._ser.write(cmd)
        try:
            self._ser.flush()
        except Exception:
            pass
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        out = bytearray()
        echo = cmd.strip()
        while time.monotonic() < deadline:
            n = self._ser.in_waiting
            chunk = self._ser.read(n if n > 0 else 1)
            if chunk:
                out.extend(chunk)
                last_data = time.monotonic()
            else:
                # The radio echoes the command at once; a remote's answer
                # only follows a TDM round trip.  Don't let the idle gap
                # start counting until something beyond the echo arrived.
                if (
                    self._beyond_echo(out, echo)
                    and (time.monotonic() - last_data) >= idle_gap
                ):
                    break
                time.sleep(0.005)
        return out.decode("ascii", errors="replace")

    @staticmethod
    def _beyond_echo(out: bytes | bytearray, echo: bytes) -> bool:
        body = bytes(out).strip()
        if echo and body.startswith(echo):
            body = body[len(echo):].strip()
        return bool(body)

    def _send_collect_for(self, cmd: bytes, duration: float) -> str:
        """Send `cmd` then read for exactly `duration` seconds.  For periodic
        output (debug reports) where an idle gap doesn't mean "finished"."""
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self._ser.write(cmd)
        try:
            self._ser.flush()
        except Exception:
            pass
        deadline = time.monotonic() + duration
        out = bytearray()
        while time.monotonic() < deadline:
            n = self._ser.in_waiting
            chunk = self._ser.read(n if n > 0 else 1)
            if chunk:
                out.extend(chunk)
            else:
                time.sleep(0.01)
        return out.decode("ascii", errors="replace")

    # ---------------------------------------- command-mode bracket
    def enter_command_mode(
        self,
        bracket: proto.CommandModeBracket | None = None,
    ) -> bool:
        """Send +++ and confirm command mode via three escalating probes.

        SiK 3.x (RFD900X2) sometimes accepts the +++ but doesn't echo OK on
        a bare ``AT`` follow-up. Trying ATI as a fallback catches that case.
        """
        b = bracket or proto.CommandModeBracket()
        with self.lock:
            try:
                self._ser.reset_input_buffer()
            except Exception:
                pass
            time.sleep(b.quiet_before)
            self._ser.write(b.plus_string)
            try:
                self._ser.flush()
            except Exception:
                pass
            time.sleep(b.quiet_after)
            # Probe 1: post-+++ banner. Most SiK builds emit "OK\r\n" here.
            deadline = time.monotonic() + b.reply_timeout
            post_plus = bytearray()
            while time.monotonic() < deadline:
                n = self._ser.in_waiting
                chunk = self._ser.read(n if n else 1)
                if chunk:
                    post_plus.extend(chunk)
                    if _has_ok(post_plus.decode("ascii", errors="replace")):
                        return True
                else:
                    if post_plus:
                        break
                    time.sleep(0.01)
            # Probe 2: AT.
            if _has_ok(self._send_collect(b"AT\r\n", timeout=b.reply_timeout)):
                return True
            # Probe 3: ATI — succeeds if the firmware returns its banner,
            # which it does in command mode but never in passthrough.
            reply = self._send_collect(b"ATI\r\n", timeout=b.reply_timeout)
            return _has_ok(reply) or bool(
                re.search(r"\b(SiK|RFD\w*|MultiComm)\b", reply)
            )

    def exit_command_mode(self) -> bool:
        with self.lock:
            reply = self._send_collect(proto.at_exit_command_mode(), timeout=1.0)
            return _has_ok(reply)

    # ---------------------------------------- parameter ops
    def read_params(self, *, remote: bool = False, timeout: float = 3.0) -> Ati5Result:
        with self.lock:
            if self.is_multicomm:
                return self._read_params_multicomm(remote=remote, timeout=timeout)
            reply = self._send_collect(proto.at_read_params(remote=remote), timeout=timeout)
            result = proto.parse_ati5(reply)
            if not result.s_params:
                raise RadioError(
                    f"no S-registers in {'remote' if remote else 'local'} ATI5 reply"
                )
            if multicomm.looks_like_multicomm(names=result.s_names.values()):
                # The banner didn't say so, but the register names do.  A
                # plain ATI5 may have been truncated: re-read in ranges,
                # keeping what we already have as a fallback.
                self.family = FAMILY_MULTICOMM
                return self._read_params_multicomm(
                    remote=remote, timeout=timeout, seed=result,
                )
            return result

    def _read_params_multicomm(
        self,
        *,
        remote: bool,
        timeout: float,
        seed: Ati5Result | None = None,
    ) -> Ati5Result:
        """Read every register via ``ATI5:x:y`` chunks (manual §3.2)."""
        node = self._node(remote)
        chunk_timeout, gap = self._remote_timing(remote, min(timeout, 1.5))
        s_params: dict[int, ParamValue] = {}
        s_names: dict[int, str] = {}
        s_meta: dict[int, ParamMeta] = {}

        def read_range(first: int, last: int) -> Ati5Result:
            reply = self._send_collect(
                proto.at_read_params(remote=remote, node=node, first=first, last=last),
                timeout=chunk_timeout, idle_gap=gap,
            )
            return proto.parse_ati5(reply)

        last_wanted = self._max_sreg if remote else multicomm.MAX_SREG
        missing: list[tuple[int, int]] = []
        chunked = False
        first = 0
        while first <= last_wanted:
            last = min(first + self.MULTICOMM_CHUNK - 1, last_wanted)
            beyond = first > multicomm.MAX_SREG
            part = read_range(first, last)
            if not part.s_params and first > 0 and not beyond:
                part = read_range(first, last)          # one retry (RF loss)
                if not part.s_params:
                    missing.append((first, last))
            if first == 0 and not part.s_params:
                # Nobody answered the first range: no remote in range, or a
                # build without ATI5:x:y.  Fall back to whatever a plain
                # ATI5 gave us (or give up for a silent remote).
                if seed is None and not remote:
                    reply = self._send_collect(proto.at_read_params(), timeout=timeout)
                    seed = proto.parse_ati5(reply)
                if seed is None or not seed.s_params:
                    raise RadioError(
                        f"no S-registers in {'remote' if remote else 'local'} "
                        "MultiComm ATI5 reply"
                    )
                break
            chunked = True
            s_params.update(part.s_params)
            s_names.update(part.s_names)
            s_meta.update(part.s_meta)
            first = last + 1
            # Newer firmware may define more than S49: probe past the end
            # (local only, once per connection) while ranges come back full.
            if (not remote and first > last_wanted and last in part.s_params
                    and last_wanted < 255
                    and (beyond or not self._probed_beyond)):
                self._probed_beyond = True
                last_wanted = min(last_wanted + self.MULTICOMM_CHUNK, 255)

        if missing and remote:
            gaps = ", ".join(f"S{a}–S{b}" for a, b in missing)
            raise RadioError(f"incomplete remote read: no reply for {gaps}")

        if seed is not None:
            for sreg, value in seed.s_params.items():
                s_params.setdefault(sreg, value)
                s_names.setdefault(sreg, seed.s_names.get(sreg, ""))
        if not remote and chunked and s_params:
            # Only trust a ranged read for the bound; a truncated plain ATI5
            # fallback would shrink every later remote read.
            self._max_sreg = max(s_params)
        if s_meta:
            if not remote:
                self._meta_cache = dict(s_meta)
                self._meta_fetched = True
        elif not remote:
            s_meta = self.read_param_meta(sorted(s_params))
        else:
            s_meta = dict(self._meta_cache)
        return Ati5Result(
            s_params=s_params,
            pin_params={},
            s_names=multicomm.canonicalize_names(s_names),
            pin_names={},
            family=FAMILY_MULTICOMM,
            s_meta=s_meta,
        )

    def read_param_meta(self, sregs: list[int]) -> dict[int, ParamMeta]:
        """Fetch each register's valid range/options with ``ATI10:n``.

        Local only (one quick round trip each) and cached for the
        connection — including a negative result — since limits are fixed
        per radio/region.  Gives up after a few registers in a row return
        no metadata, so a build without ATI10 costs ~2 s once, not 40 s
        on every load.
        """
        with self.lock:
            if self._meta_fetched:
                return dict(self._meta_cache)
            meta: dict[int, ParamMeta] = {}
            misses = 0
            for sreg in sregs:
                reply = self._send_collect(
                    proto.at_param_info(sreg), timeout=0.8, idle_gap=0.08,
                )
                part = proto.parse_ati5(reply)
                if sreg in part.s_meta:
                    meta[sreg] = part.s_meta[sreg]
                    misses = 0
                else:
                    misses += 1
                    if not meta and misses >= 3:
                        break
            self._meta_cache = meta
            self._meta_fetched = True
            return dict(meta)

    def _write_cmd(self, sreg: int, value: ParamValue, remote: bool, pin: bool) -> bytes:
        if pin:
            return proto.at_set_pin(sreg, value, remote=remote)  # type: ignore[arg-type]
        return proto.at_set_param(sreg, value, remote=remote, node=self._node(remote))

    @staticmethod
    def _reply_ok(reply: str) -> bool:
        return _has_ok(reply) and proto.reply_error(reply) is None

    def write_param(
        self,
        sreg: int,
        value: ParamValue,
        *,
        remote: bool = False,
        pin: bool = False,
        timeout: float = 1.0,
    ) -> bool:
        cmd = self._write_cmd(sreg, value, remote, pin)
        t, gap = self._remote_timing(remote, timeout)
        with self.lock:
            reply = self._send_collect(cmd, timeout=t, idle_gap=gap)
            return self._reply_ok(reply)

    def write_params_batch(
        self,
        updates: list[tuple[int, ParamValue, bool, bool]],
        *,
        timeout: float = 1.0,
    ) -> list[tuple[int, ParamValue, bool, bool, bool]]:
        """Run many writes in a single locked block — caller is responsible
        for being in command mode before calling. Returns the same tuples
        with an `ok` flag appended.
        """
        results: list[tuple[int, ParamValue, bool, bool, bool]] = []
        with self.lock:
            for sreg, value, is_remote, is_pin in updates:
                cmd = self._write_cmd(sreg, value, is_remote, is_pin)
                t, gap = self._remote_timing(is_remote, timeout)
                reply = self._send_collect(cmd, timeout=t, idle_gap=gap)
                results.append((sreg, value, is_remote, is_pin, self._reply_ok(reply)))
        return results

    def save_eeprom(self, *, remote: bool = False) -> bool:
        if self.is_multicomm:
            # MultiComm persists every ATSn= write immediately and has no
            # AT&W (manual §3.2) — nothing to do, and sending it would ERROR.
            return True
        with self.lock:
            reply = self._send_collect(proto.at_save_eeprom(remote=remote), timeout=2.0)
            return self._reply_ok(reply)

    def reboot(self, *, remote: bool = False) -> None:
        # Reboot disconnects us; no reply is expected.
        with self.lock:
            self._ser.write(proto.at_reboot(remote=remote, node=self._node(remote)))
            try:
                self._ser.flush()
            except Exception:
                pass

    def factory_reset(self, *, remote: bool = False) -> bool:
        t, gap = self._remote_timing(remote, 2.0)
        with self.lock:
            reply = self._send_collect(
                proto.at_factory_reset(remote=remote, node=self._node(remote)),
                timeout=t, idle_gap=gap,
            )
            return self._reply_ok(reply)

    def send_at(self, command: str, *, timeout: float = 1.5) -> str:
        if not command.endswith(("\r", "\n")):
            command = command + "\r\n"
        with self.lock:
            return self._send_collect(
                command.encode("ascii", errors="replace"), timeout=timeout
            )

    # Seconds to capture the MultiComm AT&T=RSSI report per one-off poll.
    # The report interval isn't documented; SiK's equivalent prints ~1 Hz.
    MULTICOMM_RSSI_WINDOW = 2.2
    # In a polling session, a report older than this counts as stale.
    RSSI_STALE_S = 3.0

    def set_rssi_report(self, on: bool) -> None:
        """Switch the ``AT&T=RSSI`` debug report on/off (command mode)."""
        with self.lock:
            self._send_collect(proto.at_rssi_report(on), timeout=0.6)
            self._rssi_text = ""
            self._rssi_last = None

    def read_rssi_report(self) -> RssiReport:
        """Session poll: parse report lines the radio printed since the last
        call, without sending anything.  Instant, so a short poll interval
        is honoured.  Between report lines the previous reading is reused
        for up to RSSI_STALE_S; after that the link state is unknown."""
        with self.lock:
            n = self._ser.in_waiting
            data = self._ser.read(n) if n > 0 else b""
            text = getattr(self, "_rssi_text", "") + data.decode("ascii", errors="replace")
            complete, _, partial = text.rpartition("\n")
            self._rssi_text = partial[-512:]
            now = time.monotonic()
            report = multicomm.parse_rssi_report(complete, node=self.remote_node)
            if report.linked is not None:
                self._rssi_last = (now, report)
                return report
            last = getattr(self, "_rssi_last", None)
            if last is not None and now - last[0] <= self.RSSI_STALE_S:
                return last[1]
            return RssiReport(units="dBm", raw=complete.strip())

    def poll_rssi(self, *, timeout: float = 1.0) -> RssiReport:
        with self.lock:
            if self.is_multicomm:
                return self._poll_rssi_multicomm()
            reply = self._send_collect(proto.at_rssi(), timeout=timeout)
            return proto.parse_ati7(reply)

    def _poll_rssi_multicomm(self) -> RssiReport:
        """Sample the ``AT&T=RSSI`` report (MultiComm's ATI7 is TDM timing),
        then switch it off again so it doesn't pollute the data stream.

        If no per-node lines appear, fall back to ``RTI`` as a link probe:
        any remote banner coming back proves the link is up.
        """
        try:
            text = self._send_collect_for(
                proto.at_rssi_report(True), self.MULTICOMM_RSSI_WINDOW,
            )
        finally:
            # Always switch the report off again, or it keeps printing
            # into the data stream after ATO.
            self._send_collect(proto.at_rssi_report(False), timeout=0.6)
        report = multicomm.parse_rssi_report(text, node=self.remote_node)
        if report.linked:
            return report
        return RssiReport(
            local_noise=report.local_noise,
            raw=text.strip(),
            units="dBm",
            linked=self.probe_remote(),
        )

    def probe_remote(self, *, timeout: float = 1.0) -> bool:
        """Cheap "is a remote answering?" check.

        SiK: a non-empty RTI5.  MultiComm: ``RTI`` (remote banner) — a full
        remote read there is ~10 ranged round trips over the air.
        """
        with self.lock:
            if not self.is_multicomm:
                try:
                    return bool(self.read_params(remote=True, timeout=timeout).s_params)
                except RadioError:
                    return False
            t, gap = self._remote_timing(True, timeout)
            reply = self._send_collect(
                proto.at_remote_identify(node=self.remote_node), timeout=t, idle_gap=gap,
            )
            lines = [
                ln.strip() for ln in reply.replace("\r", "").splitlines()
                if ln.strip() and not ln.strip().upper().startswith(("RTI", "OK"))
            ]
            return bool(lines) and proto.reply_error(reply) is None

    def identify(self, *, timeout: float = 1.0) -> dict:
        info: dict[str, object] = {}
        with self.lock:
            info["banner"] = proto.parse_banner(
                self._send_collect(proto.at_identify(), timeout=timeout)
            )
            if multicomm.looks_like_multicomm(banner=str(info["banner"])):
                self.family = FAMILY_MULTICOMM
            info["board_id"] = proto.parse_int_response(
                self._send_collect(proto.at_board_id(), timeout=timeout)
            )
            info["board_name"] = proto.board_name(info["board_id"])  # type: ignore[arg-type]
            info["freq_id"] = proto.parse_int_response(
                self._send_collect(proto.at_freq_id(), timeout=timeout)
            )
            info["bootloader_version"] = proto.parse_banner(
                self._send_collect(proto.at_bootloader_version(), timeout=timeout)
            )
        info["firmware_family"] = self.family
        return info

    def enter_bootloader(self) -> None:
        with self.lock:
            self._ser.write(proto.at_bootloader())
            try:
                self._ser.flush()
            except Exception:
                pass


# --------------------------------------------------------------------- async wrapper
def _async(method):
    """Submit `method` to ``self._executor`` instead of running it inline.

    Lets UI code call ``radio.open_port(...)`` from the GUI thread without
    blocking; the actual work happens on the radio's single worker thread.
    Qt signals emitted from that thread are delivered back to the UI via
    queued connections (Qt detects the thread mismatch automatically).
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        return self._executor.submit(method, self, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------- factory
def default_serial_factory(
    port: str,
    baud: int,
    timeout: float = 0.1,
):
    """Create a real pyserial.Serial.  Tests inject their own factory."""
    import serial  # local import keeps `from rfd import radio` cheap

    return serial.Serial(
        port=port,
        baudrate=baud,
        timeout=timeout,
        write_timeout=2.0,
    )


# --------------------------------------------------------------------- Radio (Qt)
class Radio(QObject):
    """Qt wrapper around RadioCore.  Designed to live on a worker QThread."""

    STATE_DISCONNECTED = "disconnected"
    STATE_DATA = "data"            # passthrough — bytes flow as MAVLink/raw
    STATE_COMMAND = "command"      # +++ bracket open
    STATE_BOOTLOADER = "bootloader"

    # ---- signals (delivered to UI thread via queued connections) ----
    connected = Signal(str, int)            # port, baud
    disconnected = Signal(str)              # reason
    error = Signal(str)
    log = Signal(str, int)                  # text, level (0=info, 1=warn, 2=err)
    state_changed = Signal(str)
    params_loaded = Signal(object, bool)    # Ati5Result, is_remote
    params_failed = Signal(bool, str)       # is_remote, reason
    # `object` rather than int: values can be tuples (MultiComm arrays) or
    # exceed 32 bits (MultiComm SLOTIDS is a 64-bit mask).
    write_result = Signal(int, object, bool, bool)  # sreg, value, ok, is_remote
    write_batch_done = Signal(object)            # list of (sreg, value, is_remote, is_pin, ok)
    rssi_received = Signal(object)          # RssiReport
    rx_data = Signal(bytes)                 # raw passthrough bytes
    radio_info = Signal(object)             # dict
    mavlink_radio_status = Signal(object)   # rfd.mavlink_parser.RadioStatus
    eeprom_saved = Signal(bool, bool)       # ok, is_remote
    # Emitted once a reboot() request has run to completion: came_back is
    # True when the (local) radio answered again within the wait window.
    reboot_finished = Signal(bool, bool)    # came_back, is_remote
    factory_reset_done = Signal(bool, bool)
    at_response = Signal(str, str, bool)    # cmd, response, ok

    # Internal signals — emitted from the worker thread, delivered to slots on
    # this object (which lives on the GUI thread) via a queued connection so
    # all QTimer operations run on the right thread.
    _pump_start_requested = Signal()
    _pump_stop_requested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        serial_factory: Callable[[str, int, float], _SerialLike] | None = None,
        mavlink_parsing: bool = True,
    ) -> None:
        super().__init__(parent)
        self._serial_factory = serial_factory or default_serial_factory
        self._core: RadioCore | None = None
        self._state = self.STATE_DISCONNECTED
        self._mav_parser = None
        self._mavlink_enabled = mavlink_parsing
        self._read_timer: QTimer | None = None
        self._port = ""
        self._baud = 0
        self._remote_node: int | None = None
        # Who is holding command mode open ("terminal", "rssi").  While any
        # holder remains, background operations leave the radio in command
        # mode instead of sending ATO behind the holder's back.
        self._holders: set[str] = set()
        # MultiComm AT&T=RSSI report currently enabled by an RSSI session.
        self._rssi_report_on = False
        # Single-thread executor serialises all radio I/O onto a worker so the
        # UI thread never blocks on the +++ bracket / serial reads.  Qt signals
        # emitted from this thread are delivered to the UI via queued
        # connections automatically (Qt detects the thread mismatch).
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="rfd-radio",
        )
        # Held while the worker is in command mode so the read pump (which
        # runs on the GUI thread) doesn't race with command-response reads.
        self._serial_lock = threading.Lock()
        # Marshal start/stop of the QTimer onto whichever thread owns this
        # QObject (the GUI thread) — the worker thread cannot create or
        # touch QTimers safely.
        self._pump_start_requested.connect(self._do_start_read_pump)
        self._pump_stop_requested.connect(self._do_stop_read_pump)

    @property
    def firmware_family(self) -> str:
        """``FAMILY_SIK`` / ``FAMILY_MULTICOMM`` for the connected radio."""
        return self._core.family if self._core is not None else FAMILY_SIK

    @property
    def remote_node(self) -> int | None:
        return self._remote_node

    def set_remote_node(self, node: int | None) -> None:
        """Address MultiComm RT commands to ``node`` (None = broadcast).

        Takes effect on the next queued operation.  Ignored by SiK, which
        only ever has one remote.
        """
        self._remote_node = node
        if self._core is not None:
            self._core.remote_node = node

    def shutdown(self) -> None:
        """Stop the worker thread and release resources.  Safe to call twice."""
        try:
            self._cleanup()
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # ---------------------------------------- read pump (data mode only)
    def _start_read_pump(self) -> None:
        """Thread-safe — emits a signal that's serviced on the GUI thread."""
        self._pump_start_requested.emit()

    def _stop_read_pump(self) -> None:
        self._pump_stop_requested.emit()

    @Slot()
    def _do_start_read_pump(self) -> None:
        if self._read_timer is None:
            self._read_timer = QTimer(self)
            self._read_timer.setInterval(50)
            self._read_timer.timeout.connect(self._tick_read_pump)
        self._read_timer.start()

    @Slot()
    def _do_stop_read_pump(self) -> None:
        if self._read_timer is not None:
            self._read_timer.stop()

    def _tick_read_pump(self) -> None:
        # Local copy: the worker may set self._core = None (close_port)
        # between our checks.
        core = self._core
        if core is None or self._state != self.STATE_DATA:
            return
        # Cooperative gate: if the worker thread holds the serial lock (because
        # it's in the middle of a +++ bracket or AT exchange) skip this tick.
        # Without this, the GUI thread would steal AT/OK responses out of the
        # input buffer and command-mode entry would intermittently fail.
        if not core.lock.acquire(blocking=False):
            return
        try:
            if self._core is not core or not getattr(core.serial, "is_open", True):
                return
            ser = core.serial
            n = ser.in_waiting
            if n <= 0:
                return
            data = bytes(ser.read(n))
            if not data:
                return
            self.rx_data.emit(data)
            if self._mavlink_enabled:
                if self._mav_parser is None:
                    from .mavlink_parser import RadioStatusParser
                    self._mav_parser = RadioStatusParser()
                for msg in self._mav_parser.feed(data):
                    self.mavlink_radio_status.emit(msg)
        except Exception as e:
            self.error.emit(f"read pump: {e}")
        finally:
            core.lock.release()

    def _set_state(self, s: str) -> None:
        if s != self._state:
            self._state = s
            self.state_changed.emit(s)

    def _ensure_command(self) -> bool:
        if self._core is None:
            self.error.emit("not connected")
            return False
        if self._state == self.STATE_COMMAND:
            return True
        self._stop_read_pump()
        try:
            ok = self._core.enter_command_mode()
        except Exception as e:
            # Serial failure (adapter unplugged…): surface it instead of
            # losing it inside the executor's Future.
            self.error.emit(f"serial port error: {e}")
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
            return False
        if not ok:
            self.error.emit("could not enter command mode")
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
            return False
        self._set_state(self.STATE_COMMAND)
        return True

    def _back_to_data(self) -> None:
        if self._core is None or self._holders:
            return
        try:
            self._core.exit_command_mode()
        except Exception:
            pass
        self._set_state(self.STATE_DATA)
        self._start_read_pump()

    def _cleanup(self) -> None:
        report_on = self._rssi_report_on
        self._holders.clear()
        self._rssi_report_on = False
        self._stop_read_pump()
        core = self._core
        if core is not None:
            # Take the I/O lock (bounded) so the GUI read pump isn't mid-read.
            got = core.lock.acquire(timeout=2.0)
            try:
                if got and self._state == self.STATE_COMMAND:
                    # Don't strand the radio in AT mode (no passthrough until
                    # power-cycled) when disconnecting/quitting mid-session.
                    try:
                        if report_on:
                            core.set_rssi_report(False)
                        core.exit_command_mode()
                    except Exception:
                        pass
                self._core = None
                core.serial.close()
            except Exception:
                pass
            finally:
                if got:
                    core.lock.release()
        self._mav_parser = None
        self._set_state(self.STATE_DISCONNECTED)

    # ---------------------------------------- slots: connection
    @_async
    @Slot(str, int)
    def open_port(self, port: str, baud: int) -> None:
        if self._core is not None:
            # Reconnect (double-clicked Connect, or reopen after a flash):
            # release the old handle first instead of leaking it.
            self._stop_read_pump()
            try:
                self._core.serial.close()
            except Exception:
                pass
            self._core = None
            self._holders.clear()
            self._rssi_report_on = False
        try:
            ser = self._serial_factory(port, baud, 0.1)
            self._core = RadioCore(ser)
            self._core.remote_node = self._remote_node
            self._port, self._baud = port, baud
            self.log.emit(f"opened {port} @ {baud}", 0)
        except Exception as e:
            self.error.emit(f"open port failed: {e}")
            self._cleanup()
            return

        if not self._core.enter_command_mode():
            self.log.emit("could not enter command mode (wrong baud or radio in passthrough)", 1)
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
            self.connected.emit(port, baud)
            return
        self._set_state(self.STATE_COMMAND)

        info: dict = {}
        try:
            info = self._core.identify(timeout=1.5)
            self.radio_info.emit(info)
            self.log.emit(f"identified: {info.get('board_name')} / {info.get('banner')}", 0)
        except Exception as e:
            self.log.emit(f"identify failed: {e}", 1)

        try:
            local = self._core.read_params(timeout=3.0)
            self.params_loaded.emit(local, False)
        except Exception as e:
            self.log.emit(f"read local params failed: {e}", 1)

        if info and info.get("firmware_family") != self._core.family:
            # Banner didn't reveal MultiComm but the register names did —
            # re-announce so family-aware UI (presets, RSSI source) updates.
            info = dict(info, firmware_family=self._core.family)
            self.radio_info.emit(info)
        if self._core.is_multicomm:
            self.log.emit(
                "MultiComm firmware detected — parameters persist on write "
                "(no AT&W); reboot to apply", 0,
            )

        try:
            remote = self._core.read_params(remote=True, timeout=2.0)
            self.params_loaded.emit(remote, True)
        except Exception:
            self.log.emit("no remote radio response (RTI5 timed out)", 0)

        self._back_to_data()
        self.connected.emit(port, baud)

    @_async
    @Slot()
    def close_port(self) -> None:
        self._cleanup()
        self.disconnected.emit("user requested")

    # ---------------------------------------- slots: parameter ops
    @_async
    @Slot(bool)
    def read_params(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            self.params_failed.emit(is_remote, "could not enter command mode")
            return
        try:
            res = self._core.read_params(remote=is_remote, timeout=3.0)  # type: ignore[union-attr]
            self.params_loaded.emit(res, is_remote)
        except Exception as e:
            # A remote read failing usually just means there's no linked
            # partner radio — treat it as informational, not an error.
            if is_remote:
                self.log.emit(f"no remote radio response: {e}", 0)
            else:
                self.error.emit(f"read params: {e}")
            self.params_failed.emit(is_remote, str(e))
        finally:
            self._back_to_data()

    def read_params_once(
        self,
        is_remote: bool,
        on_loaded: Callable[[object], None],
        on_failed: Callable[[str], None] | None = None,
    ) -> None:
        """Read params and call exactly one of the callbacks, once.

        Call from the GUI thread.  Both listeners are removed whichever way
        the read ends, so a failed read can't leave a stale listener that
        fires on some later, unrelated load.
        """
        def cleanup() -> None:
            for sig, fn in ((self.params_loaded, ok), (self.params_failed, fail)):
                try:
                    sig.disconnect(fn)
                except (RuntimeError, TypeError):
                    pass

        def ok(result: object, remote: bool) -> None:
            if remote != is_remote:
                return
            cleanup()
            on_loaded(result)

        def fail(remote: bool, reason: str) -> None:
            if remote != is_remote:
                return
            cleanup()
            if on_failed is not None:
                on_failed(reason)

        self.params_loaded.connect(ok)
        self.params_failed.connect(fail)
        self.read_params(is_remote)

    @_async
    @Slot(int, object, bool, bool)
    def write_param(
        self,
        sreg: int,
        value: ParamValue,
        is_remote: bool = False,
        is_pin: bool = False,
    ) -> None:
        if not self._ensure_command():
            self.write_result.emit(sreg, value, False, is_remote)
            return
        try:
            ok = self._core.write_param(  # type: ignore[union-attr]
                sreg, value, remote=is_remote, pin=is_pin
            )
            self.write_result.emit(sreg, value, ok, is_remote)
        except Exception as e:
            self.error.emit(f"write param: {e}")
            self.write_result.emit(sreg, value, False, is_remote)
        finally:
            self._back_to_data()

    @_async
    @Slot(object)
    def write_params_batch(self, updates: list[tuple[int, ParamValue, bool, bool]]) -> None:
        """Send many writes in a single command-mode bracket.

        `updates` is a list of (sreg, value, is_remote, is_pin) tuples.  Emits
        an individual `write_result` for each entry plus one `write_batch_done`
        with the full results list at the end.  Stays in command mode until
        the entire batch is processed, so a 27-write save takes one +++/ATO
        round trip instead of 27.
        """
        if not updates:
            return

        def report_all_failed() -> None:
            # Every update gets an answer, so callers tracking pending
            # writes (Save Settings) always see the batch finish.
            results = [(s, v, r, p, False) for s, v, r, p in updates]
            for sreg, value, is_remote, _pin, ok in results:
                self.write_result.emit(sreg, value, ok, is_remote)
            self.write_batch_done.emit(results)

        if not self._ensure_command():
            report_all_failed()
            return
        try:
            results = self._core.write_params_batch(list(updates))  # type: ignore[union-attr]
            for sreg, value, is_remote, is_pin, ok in results:
                self.write_result.emit(sreg, value, ok, is_remote)
            self.write_batch_done.emit(results)
        except Exception as e:
            self.error.emit(f"batch write: {e}")
            report_all_failed()
        finally:
            self._back_to_data()

    @_async
    @Slot(bool)
    def save_eeprom(self, is_remote: bool = False) -> None:
        if self._core is not None and self._core.is_multicomm:
            # No AT&W on MultiComm; skip the +++ bracket entirely.
            self.log.emit(
                f"MultiComm: {'remote' if is_remote else 'local'} parameters "
                "were saved when written (no AT&W needed)", 0,
            )
            self.eeprom_saved.emit(True, is_remote)
            return
        if not self._ensure_command():
            return
        try:
            ok = self._core.save_eeprom(remote=is_remote)  # type: ignore[union-attr]
            self.eeprom_saved.emit(ok, is_remote)
        except Exception as e:
            self.error.emit(f"save eeprom: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(bool)
    def reboot(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            self.reboot_finished.emit(False, is_remote)
            return
        came_back = is_remote
        try:
            self._core.reboot(remote=is_remote)  # type: ignore[union-attr]
            self.log.emit(
                f"rebooted {'remote' if is_remote else 'local'} radio — "
                "waiting for it to come back up…",
                0,
            )
            # ATZ doesn't reply; the radio re-boots and re-opens its UART
            # after ~3-6 seconds on SiK 3.x. We hold the executor (and
            # therefore block any subsequent user request) until either it
            # answers an AT probe or we hit the timeout. State stays as
            # COMMAND during this wait, which keeps the read pump idle.
            if not is_remote:
                # The radio left command mode, and a reboot turns its debug
                # report off; an RSSI session re-enters on its next poll.
                self._holders.discard("terminal")
                self._rssi_report_on = False
                came_back = self._wait_for_radio(timeout=10.0)
                if came_back:
                    self.log.emit("radio is back online", 0)
                else:
                    self.log.emit(
                        "radio did not respond within 10s — click Load Settings to reconnect",
                        1,
                    )
        except Exception as e:
            self.error.emit(f"reboot: {e}")
            came_back = False
        finally:
            if not is_remote:
                self._set_state(self.STATE_DATA)
                self._start_read_pump()
            else:
                # RTZ ran inside our own +++ bracket; leave it normally.
                self._back_to_data()
            self.reboot_finished.emit(came_back, is_remote)

    def _wait_for_radio(self, timeout: float = 10.0) -> bool:
        """After ATZ, poll AT until we get an OK reply or the deadline hits."""
        if self._core is None:
            return False
        deadline = time.monotonic() + timeout
        # Initial settle — SiK doesn't even open its UART for ~1s after ATZ.
        time.sleep(1.5)
        # One +++ attempt takes ≥2.2 s of guard time; don't start one that
        # can't finish before the deadline (keeps the logged timeout honest).
        min_attempt = 2.3
        bracket = proto.CommandModeBracket(
            quiet_before=1.1, quiet_after=1.1, reply_timeout=1.5
        )
        while time.monotonic() + min_attempt < deadline:
            try:
                if self._core.enter_command_mode(bracket):
                    self._core.exit_command_mode()
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    @_async
    @Slot(bool)
    def factory_reset(self, is_remote: bool = False) -> None:
        if not self._ensure_command():
            return
        try:
            ok = self._core.factory_reset(remote=is_remote)  # type: ignore[union-attr]
            self.factory_reset_done.emit(ok, is_remote)
        except Exception as e:
            self.error.emit(f"factory reset: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot()
    def poll_rssi(self) -> None:
        if not self._ensure_command():
            return
        try:
            if "rssi" in self._holders and self._core.is_multicomm:  # type: ignore[union-attr]
                # Session: the report is already streaming — just read it.
                self._enable_session_report()
                r = self._core.read_rssi_report()  # type: ignore[union-attr]
            else:
                r = self._core.poll_rssi()  # type: ignore[union-attr]
            self.rssi_received.emit(r)
        except Exception as e:
            self.error.emit(f"poll rssi: {e}")
        finally:
            self._back_to_data()

    @_async
    @Slot(str)
    def send_raw_at(self, command: str) -> None:
        """Used by the terminal tab.  Stays in command mode after — the
        terminal explicitly calls back_to_data() when the user is done."""
        if not self._ensure_command():
            return
        self._holders.add("terminal")
        try:
            reply = self._core.send_at(command)  # type: ignore[union-attr]
            ok = proto.reply_error(reply) is None
            self.at_response.emit(command, reply, ok)
        except Exception as e:
            self.error.emit(f"send at: {e}")
            return
        # Commands typed by hand that take the radio out of command mode:
        # keep our state in step so later AT lines re-enter with +++ and
        # passthrough data is read again.
        verb = command.strip().upper()
        if verb in ("ATO", "ATZ"):
            # Only the terminal's hold ends; an RSSI session re-enters
            # command mode on its next poll.
            self._holders.discard("terminal")
            if verb == "ATZ":
                self._rssi_report_on = False
            self._set_state(self.STATE_DATA)
            self._start_read_pump()
        elif verb == "AT&UPDATE":
            self._holders.clear()
            self._rssi_report_on = False
            self._set_state(self.STATE_BOOTLOADER)

    @_async
    @Slot()
    def back_to_data(self) -> None:
        """Terminal released command mode.  Other holders (an RSSI session)
        keep it open until they release it too."""
        self._holders.discard("terminal")
        if self._core is not None:
            self._back_to_data()

    @property
    def _hold_command(self) -> bool:
        return bool(self._holders)

    # ---------------------------------------- RSSI polling session
    @_async
    @Slot()
    def start_rssi_session(self) -> None:
        """Hold command mode for fast RSSI polling.

        Entering command mode costs ≥2.2 s of +++ guard time, so doing it
        per poll made any interval under ~2.5 s impossible.  While a session
        is open, SiK polls are a bare ATI7 and MultiComm polls just read the
        AT&T=RSSI lines already printed.  Serial passthrough is paused for
        the duration.
        """
        self._holders.add("rssi")
        if not self._ensure_command():
            return
        self._enable_session_report()

    def _enable_session_report(self) -> None:
        if self._core is not None and self._core.is_multicomm and not self._rssi_report_on:
            try:
                self._core.set_rssi_report(True)
                self._rssi_report_on = True
            except Exception as e:
                self.error.emit(f"RSSI report: {e}")

    @_async
    @Slot()
    def stop_rssi_session(self) -> None:
        if "rssi" not in self._holders:
            return
        self._holders.discard("rssi")
        if self._core is None:
            return
        if self._rssi_report_on and self._state == self.STATE_COMMAND:
            try:
                self._core.set_rssi_report(False)
            except Exception:
                pass
        self._rssi_report_on = False
        if self._state == self.STATE_COMMAND:
            self._back_to_data()

    @property
    def rssi_session_active(self) -> bool:
        return "rssi" in self._holders

    def assume_bootloader(self) -> None:
        """GUI thread: treat the port as owned by a bootloader (BOOT-pin
        recovery) — stop the read pump so it can't steal uploader bytes."""
        self._holders.clear()
        self._rssi_report_on = False
        self._stop_read_pump()
        self._set_state(self.STATE_BOOTLOADER)

    @_async
    @Slot()
    def enter_bootloader(self) -> None:
        if not self._ensure_command():
            return
        try:
            self._core.enter_bootloader()  # type: ignore[union-attr]
            self._set_state(self.STATE_BOOTLOADER)
            self.log.emit("entered bootloader (AT&UPDATE)", 0)
        except Exception as e:
            self.error.emit(f"enter bootloader: {e}")
