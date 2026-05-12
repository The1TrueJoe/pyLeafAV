"""
Async client for Leaf HDMI matrix switches.

All public methods use 1-indexed inputs and outputs to match the hardware
labels printed on the device (Input 1, Output 1, …).

Example::

    import asyncio
    from leafav import LeafMatrix

    async def main():
        async with LeafMatrix("192.168.1.10", model="LU862") as m:
            await m.switch_input(output=1, input=2)
            src = await m.query_av_source(output=1)
            print(f"Output 1 is showing Input {src}")

    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import protocol as proto

log = logging.getLogger(__name__)


class LeafAVError(Exception):
    """Raised on protocol errors or connection failures."""


class LeafMatrix:
    """
    Async TCP client for a Leaf HDMI AV matrix switch.

    Parameters
    ----------
    host:
        IP address of the matrix switch.
    port:
        TCP port (default 8105).
    model:
        Model name — one of the keys in :data:`leafav.MODELS`
        (``"LU642"``, ``"LU642L"``, ``"LU862"``, ``"LU862D"``, ``"LU1082"``).
    keepalive_interval:
        Seconds between keepalive packets (``LEAF[.]``).
    command_delay:
        Minimum delay between TCP commands. The Control4 driver defaults to
        250 ms (``Command Delay Milliseconds`` property).
    max_connection_age:
        Proactively reconnect before the matrix's short TCP session lifetime is
        reached. Real LU862 hardware closes otherwise-healthy sockets after
        roughly six seconds, even if keepalives are sent.
    timeout:
        Seconds to wait for connection or a query response.
    """

    DEFAULT_PORT: int = 8105

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        model: str = "LU862",
        keepalive_interval: float = 30.0,
        command_delay: float = 0.25,
        max_connection_age: Optional[float] = 4.5,
        timeout: float = 5.0,
    ) -> None:
        if model not in proto.MODELS:
            raise ValueError(
                f"Unknown model {model!r}. Valid models: {list(proto.MODELS)}"
            )
        self.host = host
        self.port = port
        self.model = model
        self.keepalive_interval = keepalive_interval
        self.command_delay = command_delay
        self.max_connection_age = max_connection_age
        self.timeout = timeout

        self._info: Dict[str, int] = proto.MODELS[model]
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._write_lock: asyncio.Lock = asyncio.Lock()
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._last_send_at: float = 0.0
        self._connected_at: float = 0.0
        # Set after device responds to the initial LEAF[.] keepalive with LEAF[*]
        self._ready: Optional[asyncio.Event] = None

        # Pending query futures keyed by (response_cmd_id, zone_byte).
        # Using a compound key means concurrent queries for *different*
        # outputs can be in-flight simultaneously without collision.
        self._pending: Dict[Tuple[int, int], asyncio.Future] = {}

        # Unsolicited-event callbacks
        self._input_change_cbs: List[Callable[[int, int], Any]] = []
        self._volume_change_cbs: List[Callable[[int, int], Any]] = []
        self._mute_change_cbs: List[Callable[[int, bool], Any]] = []

    # ------------------------------------------------------------------
    # Context-manager protocol
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "LeafMatrix":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open a TCP connection and start the reader/keepalive tasks."""
        log.info("Connecting to %s:%d (%s)", self.host, self.port, self.model)
        self._ready = asyncio.Event()
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=self.timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise LeafAVError(
                f"Cannot connect to {self.host}:{self.port}: {exc}"
            ) from exc
        self._connected_at = asyncio.get_running_loop().time()
        self._last_send_at = 0.0

        self._reader_task = asyncio.ensure_future(self._reader_loop())
        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

        # The device requires a successful LEAF[.] keepalive poll before it
        # will respond to binary commands — see OnNetworkStatusChanged in the
        # Lua driver.  Send one immediately and wait for the LEAF[*] ack.
        log.info("→ Sending initial LEAF[.] handshake…")
        await self._raw_send(proto.CMD_KEEP_ALIVE)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout)
            log.info("← Received LEAF[*] ack — device is ready")
        except asyncio.TimeoutError:
            log.warning(
                "No LEAF[*] ack from %s:%d within %.1fs; proceeding anyway",
                self.host, self.port, self.timeout,
            )

        log.info("Connected to %s:%d (%s)", self.host, self.port, self.model)

    async def disconnect(self) -> None:
        """Cancel background tasks and close the TCP connection."""
        for task in (self._reader_task, self._keepalive_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

        self._reader = self._writer = None
        self._reader_task = self._keepalive_task = None
        self._connected_at = 0.0
        log.info("Disconnected from %s:%d", self.host, self.port)

    async def reconnect(self) -> None:
        """Close the current socket, then open a fresh device connection."""
        log.info("Reconnecting to %s:%d", self.host, self.port)
        await self.disconnect()
        await self.connect()

    async def _ensure_connection_fresh(self) -> None:
        """
        Ensure there is an open socket that is not close to the device timeout.

        LU862 test hardware closes TCP sessions after about six seconds even
        when keepalive acks are flowing. Reconnecting before that limit keeps
        longer query batches (GUI refresh / diagnostics) reliable.
        """
        loop = asyncio.get_running_loop()
        stale = (
            self.max_connection_age is not None
            and self._connected_at
            and (loop.time() - self._connected_at) >= self.max_connection_age
        )
        closed = self._writer is None or self._writer.is_closing()
        if not stale and not closed:
            return

        async with self._reconnect_lock:
            loop = asyncio.get_running_loop()
            stale = (
                self.max_connection_age is not None
                and self._connected_at
                and (loop.time() - self._connected_at) >= self.max_connection_age
            )
            closed = self._writer is None or self._writer.is_closing()
            if stale or closed:
                await self.reconnect()

    @property
    def is_connected(self) -> bool:
        """True if the TCP connection is currently open."""
        return self._writer is not None and not self._writer.is_closing()

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        """
        Read raw bytes from the device and extract LEAF[...] messages.

        The Lua driver strips all \\n characters and then scans the accumulated
        buffer for ``LEAF(.+)`` patterns, handling multiple messages per TCP
        segment safely.  We replicate that approach here.
        """
        assert self._reader is not None
        buf = ""
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    self._connection_lost("Device closed the connection")
                    break
                log.debug("← raw %d bytes: %s", len(chunk),
                          chunk.hex(" ", 1) if len(chunk) <= 64
                          else chunk[:64].hex(" ", 1) + " …")
                buf += chunk.decode("ascii", errors="replace")
                buf = buf.replace("\r", "").replace("\n", "")

                # Keep-alive ack — may appear before any LEAF[ messages
                if "[*]" in buf:
                    log.debug("← KEEP_ALIVE_ACK")
                    if self._ready is not None:
                        self._ready.set()
                    buf = buf.replace("[*]", "")

                # Extract all complete LEAF[XX,YY,ZZ] messages from the buffer
                while True:
                    start = buf.find("LEAF[")
                    if start == -1:
                        buf = ""  # no LEAF prefix at all — discard
                        break
                    buf = buf[start:]    # discard anything before LEAF[
                    end = buf.find("]")  # find the closing bracket
                    if end == -1:
                        break            # incomplete — wait for more data
                    message = buf[5:end]  # content between LEAF[ and ]
                    buf = buf[end + 1:]  # advance past this message
                    full = f"LEAF[{message}]"
                    log.debug("← %s", full)
                    self._dispatch_msg(message)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Reader loop error: %s", exc)

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                log.debug("→ KEEP_ALIVE (periodic)")
                await self._raw_send(proto.CMD_KEEP_ALIVE)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _raw_send(self, data: bytes) -> None:
        async with self._write_lock:
            if self._writer is None:
                raise LeafAVError("Not connected")
            if self._writer.is_closing():
                raise LeafAVError("Connection is closed")

            now = asyncio.get_running_loop().time()
            elapsed = now - self._last_send_at
            if self._last_send_at and elapsed < self.command_delay:
                delay = self.command_delay - elapsed
                log.debug("command-delay sleep %.3fs", delay)
                await asyncio.sleep(delay)

            log.debug("→ raw %d bytes: %s", len(data), data.hex(" ", 1))
            try:
                self._writer.write(data)
                await self._writer.drain()
                self._last_send_at = asyncio.get_running_loop().time()
            except (ConnectionError, OSError) as exc:
                self._connection_lost(f"Send failed: {exc}")
                raise LeafAVError(f"Send failed: {exc}") from exc

    async def _send(self, cmd: int, param: int, zone_byte: int) -> None:
        await self._ensure_connection_fresh()
        pkt = proto.pack_network_command(cmd, param, zone_byte)
        log.debug(
            "→ CMD cmd=0x%02X param=0x%02X zone=0x%02X packet=%r",
            cmd,
            param,
            zone_byte,
            pkt,
        )
        await self._raw_send(pkt)

    async def _query(
        self,
        query_cmd: int,
        response_cmd: int,
        zone_byte: int,
        param: int = 0x00,
        response_zone_byte: Optional[int] = None,
    ) -> Tuple[int, int]:
        """
        Send *query_cmd* and await the matching response.

        Returns ``(value, zone_byte)`` from the device response.
        """
        if response_zone_byte is None:
            response_zone_byte = zone_byte

        last_error: Optional[LeafAVError] = None
        for attempt in range(2):
            await self._ensure_connection_fresh()
            key = (response_cmd, response_zone_byte)
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[Tuple[int, int]] = loop.create_future()
            self._pending[key] = fut
            try:
                await self._send(query_cmd, param, zone_byte)
                return await asyncio.wait_for(
                    asyncio.shield(fut), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                last_error = LeafAVError(
                    f"Timed out waiting for response "
                    f"0x{response_cmd:02X} on zone 0x{response_zone_byte:02X}"
                )
            except LeafAVError as exc:
                last_error = exc
            finally:
                self._pending.pop(key, None)
                if fut.done() and not fut.cancelled():
                    # If send failed after the reader marked this future with
                    # an exception, retrieve it here so asyncio does not emit
                    # "Future exception was never retrieved" during retry.
                    fut.exception()
                elif not fut.done():
                    fut.cancel()

            if attempt == 0:
                log.debug("Retrying query after reconnect: %s", last_error)
                await self.reconnect()

        assert last_error is not None
        raise last_error

    def _connection_lost(self, reason: str) -> None:
        """Mark the connection lost, fail all pending queries, and stop keepalive."""
        expected_short_session = (
            self.max_connection_age is not None
            and (
                reason == "Device closed the connection"
                or reason.startswith("Send failed:")
            )
        )
        if expected_short_session:
            log.info("%s; will reconnect on the next command", reason)
        else:
            log.warning(reason)
        exc = LeafAVError(reason)
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self._writer and not self._writer.is_closing():
            self._writer.close()

    def _dispatch(self, line: str) -> None:
        """Legacy single-line dispatcher (kept for Serial-mode compatibility)."""
        # Keep-alive ack
        if "[*]" in line:
            if self._ready is not None:
                self._ready.set()
            return
        parsed = proto.parse_response("LEAF[" + line + "]")
        if parsed is None:
            log.debug("Unrecognised response: %r", line)
            return
        cmd_id, value, zone_byte = parsed
        self._handle_parsed(cmd_id, value, zone_byte)

    def _dispatch_msg(self, content: str) -> None:
        """
        Dispatch the *content* between ``LEAF[`` and ``]``.

        *content* is the raw hex string, e.g. ``"35,01,00"``.
        """
        parsed = proto.parse_response(f"LEAF[{content}]")
        if parsed is None:
            log.debug("Unrecognised LEAF content: %r", content)
            return
        cmd_id, value, zone_byte = parsed
        log.debug("parsed → cmd=0x%02X value=0x%02X zone=0x%02X", cmd_id, value, zone_byte)
        self._handle_parsed(cmd_id, value, zone_byte)

    def _handle_parsed(self, cmd_id: int, value: int, zone_byte: int) -> None:
        """Common handling for a fully parsed device response."""
        # Satisfy a pending query if one matches.  A response zone of -1 is a
        # wildcard for diagnostic commands whose third byte is returned data.
        key = (cmd_id, zone_byte)
        any_zone_key = (cmd_id, -1)
        if key in self._pending or any_zone_key in self._pending:
            fut = self._pending[key] if key in self._pending else self._pending[any_zone_key]
            if not fut.done():
                fut.set_result((value, zone_byte))
            return

        # Fire unsolicited-event callbacks
        output = proto.byte_to_output(zone_byte)

        if cmd_id in (0x47, 0x48, 0x5D, 0x6D):          # AV source changed
            input_ = proto.byte_to_input(value)
            for cb in self._input_change_cbs:
                _safe_call(cb, output, input_)

        elif cmd_id in (0x40, 0x41, 0x59):               # Volume changed
            level = proto.device_to_level(value)
            for cb in self._volume_change_cbs:
                _safe_call(cb, output, level)

        elif cmd_id in (0x5A, 0x5B):                     # Mute changed
            muted = cmd_id == 0x5A
            for cb in self._mute_change_cbs:
                _safe_call(cb, output, muted)

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_input_change(self, callback: Callable[[int, int], Any]) -> None:
        """
        Register a callback for unsolicited input-routing changes.

        The callback receives ``(output: int, input: int)`` (both 1-indexed).
        """
        self._input_change_cbs.append(callback)

    def on_volume_change(self, callback: Callable[[int, int], Any]) -> None:
        """
        Register a callback for unsolicited volume changes.

        The callback receives ``(output: int, level: int)`` where level is 0-100.
        """
        self._volume_change_cbs.append(callback)

    def on_mute_change(self, callback: Callable[[int, bool], Any]) -> None:
        """
        Register a callback for unsolicited mute-state changes.

        The callback receives ``(output: int, muted: bool)``.
        """
        self._mute_change_cbs.append(callback)

    # ------------------------------------------------------------------
    # Model info properties
    # ------------------------------------------------------------------

    @property
    def video_inputs(self) -> int:
        """Number of video inputs on this model."""
        return self._info["video_inputs"]

    @property
    def video_outputs(self) -> int:
        """Number of video outputs on this model."""
        return self._info["video_outputs"]

    @property
    def audio_outputs(self) -> int:
        """Number of analogue audio outputs on this model (0 for LU642L)."""
        return self._info["audio_outputs"]

    @property
    def hdbt_outputs(self) -> int:
        """Number of HDBaseT extender output ports (0 for HDMI-only models)."""
        return self._info.get("hdbt_outputs", 0)

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    def _check_output(self, output: int) -> None:
        if not 1 <= output <= self._info["video_outputs"]:
            raise ValueError(
                f"Output {output} out of range 1–{self._info['video_outputs']}"
            )

    def _check_input(self, input_: int) -> None:
        if not 1 <= input_ <= self._info["video_inputs"]:
            raise ValueError(
                f"Input {input_} out of range 1–{self._info['video_inputs']}"
            )

    # ------------------------------------------------------------------
    # Public API — AV routing
    # ------------------------------------------------------------------

    async def switch_input(self, output: int, input: int) -> None:
        """Route *input* to *output* (both 1-indexed)."""
        self._check_output(output)
        self._check_input(input)
        await self._send(
            proto.CMD_SWITCH_AV,
            proto.input_to_byte(input),
            proto.output_to_byte(output),
        )

    async def disconnect_av(self, output: int) -> None:
        """Disconnect (blank) the AV output."""
        self._check_output(output)
        await self._send(
            proto.CMD_DISCONNECT_AV, 0x00, proto.output_to_byte(output)
        )

    async def set_audio_input(self, output: int, input: int) -> None:
        """Route audio *input* to *output* independently of video."""
        self._check_output(output)
        self._check_input(input)
        await self._send(
            proto.CMD_SWITCH_AUDIO,
            proto.input_to_byte(input),
            proto.output_to_byte(output),
        )

    async def disconnect_audio(self, output: int) -> None:
        """Disconnect the audio output only."""
        self._check_output(output)
        await self._send(
            proto.CMD_DISCONNECT_AUDIO, 0x00, proto.output_to_byte(output)
        )

    # ------------------------------------------------------------------
    # Public API — Volume / audio
    # ------------------------------------------------------------------

    async def mute(self, output: int) -> None:
        """Mute *output*."""
        self._check_output(output)
        await self._send(proto.CMD_MUTE_ON, 0x00, proto.output_to_byte(output))

    async def unmute(self, output: int) -> None:
        """Unmute *output*."""
        self._check_output(output)
        await self._send(proto.CMD_MUTE_OFF, 0x00, proto.output_to_byte(output))

    async def set_volume(self, output: int, level: int) -> None:
        """
        Set volume on *output* to *level* (0–100).

        The level is converted to the device's 0x00–0xFF scale using a linear
        mapping identical to the Control4 driver.
        """
        self._check_output(output)
        await self._send(
            proto.CMD_SET_VOLUME,
            proto.level_to_device(level),
            proto.output_to_byte(output),
        )

    async def volume_up(self, output: int) -> None:
        """Send a single volume-up pulse to *output*."""
        self._check_output(output)
        await self._send(proto.CMD_VOL_UP, 0x00, proto.output_to_byte(output))

    async def volume_down(self, output: int) -> None:
        """Send a single volume-down pulse to *output*."""
        self._check_output(output)
        await self._send(proto.CMD_VOL_DOWN, 0x00, proto.output_to_byte(output))

    async def set_bass(self, output: int, level: int) -> None:
        """
        Set bass level on *output* (device range: 31–225).

        Values outside the range are clamped automatically.
        """
        self._check_output(output)
        await self._send(
            proto.CMD_SET_BASS,
            max(31, min(225, level)),
            proto.output_to_byte(output),
        )

    async def set_treble(self, output: int, level: int) -> None:
        """
        Set treble level on *output* (device range: 31–225).

        Values outside the range are clamped automatically.
        """
        self._check_output(output)
        await self._send(
            proto.CMD_SET_TREBLE,
            max(31, min(225, level)),
            proto.output_to_byte(output),
        )

    # ------------------------------------------------------------------
    # Public API — Queries
    # ------------------------------------------------------------------

    async def query_av_source(self, output: int) -> int:
        """
        Return the input currently routed to *output* (1-indexed).

        Sends ``CMD_QUERY_AV_SOURCE`` and awaits the ``RESP_RETURN_AV_SOURCE``
        response.
        """
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_AV_SOURCE,
            proto.RESP_RETURN_AV_SOURCE,
            zone_byte,
        )
        return proto.byte_to_input(value)

    async def query_av_lock(self, output: int) -> int:
        """Return the input that *output* is locked to, or ``0`` if unlocked."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_AV_LOCK,
            proto.RESP_RETURN_AV_LOCK,
            zone_byte,
        )
        return proto.byte_to_input(value)

    async def query_audio_source(self, output: int) -> int:
        """Return the audio input routed to *output*, or ``0`` if disconnected."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_AUDIO,
            proto.RESP_RETURN_AUDIO,
            zone_byte,
        )
        return proto.byte_to_input(value)

    async def query_volume(self, output: int) -> int:
        """Return the current volume (0–100) for *output*."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_VOLUME,
            proto.RESP_RETURN_VOLUME,
            zone_byte,
        )
        return proto.device_to_level(value)

    async def query_mute(self, output: int) -> bool:
        """Return ``True`` if *output* is currently muted."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_MUTE,
            proto.RESP_RETURN_MUTE,
            zone_byte,
        )
        return bool(value)

    async def query_bass(self, output: int) -> int:
        """Return the raw bass byte (device range normally 31–225)."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_BASS,
            proto.RESP_RETURN_BASS,
            zone_byte,
        )
        return value

    async def query_treble(self, output: int) -> int:
        """Return the raw treble byte (device range normally 31–225)."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_TREBLE,
            proto.RESP_RETURN_TREBLE,
            zone_byte,
        )
        return value

    async def query_balance(self, output: int) -> int:
        """Return the raw balance byte (device range normally 31–225)."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_BALANCE,
            proto.RESP_RETURN_BALANCE,
            zone_byte,
        )
        return value

    async def query_delay(self, output: int) -> int:
        """Return the audio delay in milliseconds for *output*."""
        self._check_output(output)
        zone_byte = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_DELAY,
            proto.RESP_RETURN_DELAY,
            zone_byte,
        )
        return value

    async def query_status(self, subcode: int) -> int:
        """Return the raw value for a ``CMD_STATUS`` subcode."""
        value, _ = await self._query(
            proto.CMD_STATUS,
            proto.RESP_STATUS,
            subcode,
            param=subcode,
            response_zone_byte=subcode,
        )
        return value

    async def query_packet(
        self,
        query_cmd: int,
        response_cmd: int,
        zone_byte: int,
        param: int = 0x00,
        response_zone_byte: Optional[int] = None,
    ) -> Tuple[int, int]:
        """
        Low-level read-only query helper for driver-exposed diagnostics.

        Pass ``response_zone_byte=-1`` to accept the first response with the
        requested response command regardless of its third byte.
        """
        return await self._query(
            query_cmd,
            response_cmd,
            zone_byte,
            param=param,
            response_zone_byte=response_zone_byte,
        )

    async def get_routing(self) -> Dict[int, int]:
        """
        Query and return the full AV routing table.

        Returns a dict ``{output: input}`` for every output (both 1-indexed).
        Outputs that fail to respond are omitted (a warning is logged).
        """
        results: Dict[int, int] = {}
        for out in range(1, self._info["video_outputs"] + 1):
            try:
                results[out] = await self.query_av_source(out)
            except LeafAVError as exc:
                log.warning("Could not query output %d: %s", out, exc)
        return results

    # ------------------------------------------------------------------
    # Public API — HDBaseT / EDID diagnostics
    # ------------------------------------------------------------------

    async def query_hdbt_cable_length(self, output: int) -> int:
        """Return HDBaseT cable length in metres for an HDBaseT *output*.

        Values below 20 mean "< 20 m" per Control4 driver convention.
        """
        zone = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_HDBT_CABLE_LENGTH,
            proto.RESP_HDBT_CABLE_LENGTH,
            zone,
        )
        return value

    async def query_hdbt_link_status(self, output: int) -> int:
        """Return the HDBaseT link-status code for *output*.

        Codes: 0 = no link, 1 = good, 2 = low-power, 3 = Ethernet-only,
        4 = quality warning.
        """
        zone = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_HDBT_LINK_STATUS,
            proto.RESP_HDBT_LINK_STATUS,
            zone,
        )
        return value

    async def query_ir_status(self, output: int) -> int:
        """Return the IR hardware-type code for an HDBaseT *output*.

        Codes: 2 = IR transmitter (2-pin), 3 = direct drive, 4 = IR detector
        (3-pin), 5 = IR detector (alt 3-pin).
        """
        zone = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_IR_STATUS,
            proto.RESP_IR_STATUS,
            zone,
        )
        return value

    async def query_zone_edid_group(self, output: int) -> bool:
        """Return ``True`` if *output* is a member of the advanced EDID group."""
        self._check_output(output)
        zone = proto.output_to_byte(output)
        value, _ = await self._query(
            proto.CMD_QUERY_ZONE_EDID_GROUP,
            proto.RESP_ZONE_EDID_GROUP,
            zone,
        )
        return bool(value)

    async def query_source_edid_group(self, source: int) -> bool:
        """Return ``True`` if *source* input is a member of the advanced EDID group."""
        self._check_input(source)
        # The device returns [0x7B, membership, source_number]
        value, _ = await self._query(
            proto.CMD_QUERY_SOURCE_EDID_GROUP,
            proto.RESP_SOURCE_EDID_GROUP,
            0x00,
            param=source,
            response_zone_byte=-1,
        )
        return bool(value)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_call(cb: Callable, *args: Any) -> None:
    """Call *cb* with *args*, logging but not re-raising any exception."""
    try:
        cb(*args)
    except Exception as exc:
        log.warning("Event callback raised an exception: %s", exc)
