"""
Dear PyGui application for controlling Leaf HDMI matrix switches.

Architecture
------------
* asyncio event loop runs on a background daemon thread so the library's
  coroutines work normally.
* The Dear PyGui render loop runs on the main thread (required by DPG).
* All button/slider callbacks submit coroutines to the background loop via
  ``asyncio.run_coroutine_threadsafe()``.
* Unsolicited device events (routing changes, volume, mute) arrive on the
    asyncio thread and enqueue UI updates for the main DPG thread.

Usage::

    leafav-gui
"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
import logging
import os
from queue import Empty, SimpleQueue
import threading
from typing import Any, Callable, Dict, Optional, Tuple

import dearpygui.dearpygui as dpg

from .client import LeafMatrix, LeafAVError
from .protocol import MODELS

log = logging.getLogger(__name__)

_main_thread_id = threading.get_ident()
_ui_queue: SimpleQueue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = SimpleQueue()


def _on_ui_thread() -> bool:
    return threading.get_ident() == _main_thread_id


def _ui_call(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Run *func* on the DPG/main thread, or queue it for the next frame."""
    if _on_ui_thread():
        func(*args, **kwargs)
    else:
        _ui_queue.put((func, args, kwargs))


def _drain_ui_queue() -> None:
    """Apply queued UI changes from the asyncio/device thread."""
    while True:
        try:
            func, args, kwargs = _ui_queue.get_nowait()
        except Empty:
            break
        try:
            func(*args, **kwargs)
        except Exception:
            log.exception("GUI update failed")

# ---------------------------------------------------------------------------
# Background asyncio loop
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()


def _run_loop() -> None:
    asyncio.set_event_loop(_loop)
    _loop.run_forever()


threading.Thread(target=_run_loop, daemon=True, name="leafav-async").start()


def _submit(coro) -> "asyncio.Future":
    """Submit a coroutine to the background asyncio loop from any thread."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)

    def _done(done_fut) -> None:
        try:
            exc = done_fut.exception()
        except (asyncio.CancelledError, FutureCancelledError):
            return
        if exc is not None:
            log.error(
                "Async GUI task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            _set_status_err(f"Error: {exc}")
            _ui_call(_set_busy, False)

    fut.add_done_callback(_done)
    return fut


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class _AppState:
    """Mutable state shared between GUI callbacks and async tasks."""

    def __init__(self) -> None:
        self.matrix: Optional[LeafMatrix] = None
        self.n_outputs: int = 0
        self.n_inputs: int = 0
        self.n_audio: int = 0
        self.n_hdbt: int = 0
        # 1-indexed dicts populated from device queries
        self.routing: Dict[int, int] = {}
        self.volumes: Dict[int, int] = {}
        self.mutes: Dict[int, bool] = {}
        # diagnostics
        self.fw_version: str = "—"
        self.fw_server_version: str = "—"
        self.compat_mode: str = "—"
        self.edid_group_mode: str = "—"
        self.update_status: str = "—"
        self.hdbt_cable: Dict[int, str] = {}
        self.hdbt_link: Dict[int, str] = {}
        self.ir_status: Dict[int, str] = {}
        self.zone_edid: Dict[int, str] = {}
        self.source_edid: Dict[int, str] = {}


_state = _AppState()


# ---------------------------------------------------------------------------
# DPG themes  (created once in _build_ui before any widget)
# ---------------------------------------------------------------------------

_theme_active: int = 0   # green  — active route cell
_theme_pending: int = 0  # amber  — switch in flight


def _create_themes() -> None:
    global _theme_active, _theme_pending
    with dpg.theme() as _theme_active:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (20,  150,  70, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (30,  185,  90, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (10,  120,  55, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Text,          (255, 255, 255, 255))
    with dpg.theme() as _theme_pending:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button,        (170, 110,   0, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (210, 140,   0, 255))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive,  (130,  85,   0, 255))
            dpg.add_theme_color(dpg.mvThemeCol_Text,          (255, 255, 255, 255))


# ---------------------------------------------------------------------------
# DPG tag helpers
# ---------------------------------------------------------------------------

def _cell_tag(output: int, input_: int) -> str:
    return f"cell__{output}__{input_}"


def _vol_tag(output: int) -> str:
    return f"vol__{output}"


def _mute_tag(output: int) -> str:
    return f"mute__{output}"


def _route_label_tag(output: int) -> str:
    return f"route_label__{output}"


def _diag_tag(key: str) -> str:
    return f"diag__{key}"


# ---------------------------------------------------------------------------
# Status bar helpers
# ---------------------------------------------------------------------------

def _set_status(msg: str, color: tuple = (200, 200, 200, 255)) -> None:
    _ui_call(_set_status_now, msg, color)


def _set_status_now(msg: str, color: tuple = (200, 200, 200, 255)) -> None:
    if dpg.does_item_exist("status_bar"):
        dpg.set_value("status_bar", msg)
        dpg.configure_item("status_bar", color=color)


def _set_status_err(msg: str) -> None:
    _set_status(msg, color=(255, 100, 100, 255))


def _set_status_ok(msg: str) -> None:
    _set_status(msg, color=(100, 220, 100, 255))


# ---------------------------------------------------------------------------
# Grid helpers
# ---------------------------------------------------------------------------

def _highlight_active_cell(output: int, active_input: int) -> None:
    """Mark the active cell button for *output* and clear the others."""
    _ui_call(_highlight_active_cell_now, output, active_input)


def _highlight_active_cell_now(output: int, active_input: int) -> None:
    """UI-thread implementation for marking the active routing cell."""
    global _theme_active
    for inp in range(1, _state.n_inputs + 1):
        tag = _cell_tag(output, inp)
        if dpg.does_item_exist(tag):
            if inp == active_input:
                dpg.configure_item(tag, label="●")
                dpg.bind_item_theme(tag, _theme_active)
            else:
                dpg.configure_item(tag, label="○")
                dpg.bind_item_theme(tag, 0)
    if dpg.does_item_exist(_route_label_tag(output)):
        dpg.set_value(
            _route_label_tag(output),
            f"In {active_input}" if active_input else "—",
        )


def _set_cell_pending_now(output: int, input_: int) -> None:
    """Immediately colour a routing cell amber while a switch is in-flight."""
    global _theme_pending
    tag = _cell_tag(output, input_)
    if dpg.does_item_exist(tag):
        dpg.configure_item(tag, label="◌")
        dpg.bind_item_theme(tag, _theme_pending)


def _mark_output_unknown(output: int) -> None:
    """Show a query failure for one output without hiding the whole table."""
    _ui_call(_mark_output_unknown_now, output)


def _mark_output_unknown_now(output: int) -> None:
    for inp in range(1, _state.n_inputs + 1):
        tag = _cell_tag(output, inp)
        if dpg.does_item_exist(tag):
            dpg.configure_item(tag, label="?")
            dpg.bind_item_theme(tag, 0)
    lbl = _route_label_tag(output)
    if dpg.does_item_exist(lbl):
        dpg.set_value(lbl, "?")


def _reset_grid_cells() -> None:
    _ui_call(_reset_grid_cells_now)


def _reset_grid_cells_now() -> None:
    for out in range(1, _state.n_outputs + 1):
        for inp in range(1, _state.n_inputs + 1):
            tag = _cell_tag(out, inp)
            if dpg.does_item_exist(tag):
                dpg.configure_item(tag, label="○")
                dpg.bind_item_theme(tag, 0)
        lbl = _route_label_tag(out)
        if dpg.does_item_exist(lbl):
            dpg.set_value(lbl, "—")


def _set_busy(busy: bool) -> None:
    """Enable/disable top-level controls while a connect/disconnect is active."""
    _set_item_enabled("btn_connect", not busy)
    _set_item_enabled("btn_refresh", not busy and _state.matrix is not None)
    _set_item_enabled("btn_refresh_diag", not busy and _state.matrix is not None)


def _set_item_enabled(tag: str, enabled: bool) -> None:
    if not dpg.does_item_exist(tag):
        return
    if enabled:
        dpg.enable_item(tag)
    else:
        dpg.disable_item(tag)


def _set_connected_ui(connected: bool) -> None:
    if dpg.does_item_exist("btn_connect"):
        dpg.configure_item(
            "btn_connect",
            label="Disconnect" if connected else "Connect",
            callback=_on_disconnect_btn if connected else _on_connect_btn,
        )
    _set_item_enabled("btn_connect", True)
    _set_item_enabled("btn_refresh", connected)
    _set_item_enabled("btn_refresh_diag", connected)


# ---------------------------------------------------------------------------
# Diagnostic label lookup tables
# ---------------------------------------------------------------------------

_HDBT_LINK_LABELS: Dict[int, str] = {
    0: "No link",
    1: "Good link",
    2: "Low power mode",
    3: "Ethernet only",
    4: "Quality warning",
}

_IR_STATUS_LABELS: Dict[int, str] = {
    2: "IR transmitter (2-pin)",
    3: "Direct drive input",
    4: "IR detector (3-pin)",
    5: "IR detector (3-pin alt)",
}

_EDID_GROUP_MODE_LABELS: Dict[int, str] = {
    0: "Off — normal EDID",
    1: "Active — fixed EDID",
    2: "Active — calibration EDID",
}

_UPDATE_STATUS_LABELS: Dict[int, str] = {
    0: "No file available",
    1: "Up to date",
    2: "Update available",
    3: "Update triggered",
}


# ---------------------------------------------------------------------------
# Unsolicited-event callbacks (called from the asyncio thread)
# ---------------------------------------------------------------------------

def _on_input_change(output: int, input_: int) -> None:
    _state.routing[output] = input_
    _highlight_active_cell(output, input_)
    _set_status_ok(f"Out {output} \u2192 In {input_} (device push)")


def _on_volume_change(output: int, level: int) -> None:
    _state.volumes[output] = level
    tag = _vol_tag(output)
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, level)


def _on_mute_change(output: int, muted: bool) -> None:
    _state.mutes[output] = muted
    tag = _mute_tag(output)
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, muted)


# ---------------------------------------------------------------------------
# Async tasks called from GUI callbacks
# ---------------------------------------------------------------------------

async def _do_switch(output: int, input_: int) -> None:
    if not _state.matrix:
        return
    try:
        await _state.matrix.switch_input(output, input_)
        _state.routing[output] = input_
        _highlight_active_cell(output, input_)
        _set_status_ok(f"Out {output} → In {input_}")
    except (LeafAVError, ValueError) as exc:
        _set_status_err(str(exc))        # Revert the pending cell — re-query the actual active source
        try:
            actual = await _state.matrix.query_av_source(output)
            _state.routing[output] = actual
            _highlight_active_cell(output, actual)
        except LeafAVError:
            _mark_output_unknown(output)

async def _do_set_volume(output: int, level: int) -> None:
    if not _state.matrix:
        return
    try:
        await _state.matrix.set_volume(output, level)
    except (LeafAVError, ValueError) as exc:
        _set_status_err(str(exc))


async def _do_toggle_mute(output: int, muted: bool) -> None:
    if not _state.matrix:
        return
    try:
        if muted:
            await _state.matrix.mute(output)
        else:
            await _state.matrix.unmute(output)
    except (LeafAVError, ValueError) as exc:
        _set_status_err(str(exc))


async def _do_refresh_all() -> None:
    if not _state.matrix:
        return
    _set_status("Querying device…")

    route_ok = 0
    route_errors = 0
    for out in range(1, _state.n_outputs + 1):
        try:
            inp = await _state.matrix.query_av_source(out)
            _state.routing[out] = inp
            _highlight_active_cell(out, inp)
            route_ok += 1
        except LeafAVError as exc:
            route_errors += 1
            log.warning("Could not query output %d: %s", out, exc)
            _mark_output_unknown(out)

    audio_ok = 0
    audio_errors = 0
    for out in range(1, _state.n_outputs + 1):
        try:
            vol = await _state.matrix.query_volume(out)
            _state.volumes[out] = vol
            _ui_call(_set_item_value, _vol_tag(out), vol)
            audio_ok += 1
        except LeafAVError as exc:
            audio_errors += 1
            log.warning("Could not query volume for output %d: %s", out, exc)
        try:
            muted = await _state.matrix.query_mute(out)
            _state.mutes[out] = muted
            _ui_call(_set_item_value, _mute_tag(out), muted)
            audio_ok += 1
        except LeafAVError as exc:
            audio_errors += 1
            log.warning("Could not query mute for output %d: %s", out, exc)

    if route_errors or audio_errors:
        _set_status_err(
            f"Refresh finished with {route_errors} routing and {audio_errors} audio errors "
            f"({route_ok}/{_state.n_outputs} routes read)."
        )
    else:
        _set_status_ok(
            f"Ready — {route_ok}/{_state.n_outputs} routes, "
            f"{audio_ok // 2}/{_state.n_outputs} audio rows refreshed"
        )


async def _do_refresh_diagnostics() -> None:
    """Query all driver-readable diagnostics and push results into the Diagnostics tab."""
    if not _state.matrix:
        return
    m = _state.matrix
    _set_status("Querying diagnostics\u2026")

    async def _try(coro: Any) -> Any:
        try:
            return await coro
        except LeafAVError:
            return None

    def _dset(key: str, val: str) -> None:
        _ui_call(_set_item_value, _diag_tag(key), val)

    # ── Firmware ──────────────────────────────────────────────────────────
    maj = await _try(m.query_status(0x10))
    mn  = await _try(m.query_status(0x11))
    fw  = f"{maj:x}.{mn:x}" if maj is not None and mn is not None else "\u2014"
    _state.fw_version = fw
    _dset("fw_version", fw)

    smaj = await _try(m.query_status(0x14))
    smn  = await _try(m.query_status(0x15))
    sfw  = f"{smaj:x}.{smn:x}" if smaj is not None and smn is not None else "\u2014"
    _state.fw_server_version = sfw
    _dset("fw_server_version", sfw)

    # ── Device status ─────────────────────────────────────────────────────
    cm = await _try(m.query_status(0x30))
    _state.compat_mode = "Enabled" if cm == 1 else ("Disabled" if cm == 0 else "\u2014")
    _dset("compat_mode", _state.compat_mode)

    gm = await _try(m.query_status(0x68))
    _state.edid_group_mode = _EDID_GROUP_MODE_LABELS.get(gm, "\u2014") if gm is not None else "\u2014"
    _dset("edid_group_mode", _state.edid_group_mode)

    us = await _try(m.query_status(0xB9))
    _state.update_status = _UPDATE_STATUS_LABELS.get(us, "\u2014") if us is not None else "\u2014"
    _dset("update_status", _state.update_status)

    # ── HDBaseT ───────────────────────────────────────────────────────────
    for out in range(1, _state.n_hdbt + 1):
        ln = await _try(m.query_hdbt_cable_length(out))
        cable_str = ("< 20 m" if ln < 20 else f"~{ln} m") if ln is not None else "\u2014"
        _state.hdbt_cable[out] = cable_str
        _dset(f"hdbt_cable_{out}", cable_str)

        lk = await _try(m.query_hdbt_link_status(out))
        link_str = _HDBT_LINK_LABELS.get(lk, f"raw {lk}") if lk is not None else "\u2014"
        _state.hdbt_link[out] = link_str
        _dset(f"hdbt_link_{out}", link_str)

        ir = await _try(m.query_ir_status(out))
        ir_str = _IR_STATUS_LABELS.get(ir, f"raw {ir}") if ir is not None else "\u2014"
        _state.ir_status[out] = ir_str
        _dset(f"ir_status_{out}", ir_str)

    # ── EDID group membership ─────────────────────────────────────────────
    for out in range(1, _state.n_outputs + 1):
        gv = await _try(m.query_zone_edid_group(out))
        edid_str = ("Member" if gv else "Not member") if gv is not None else "\u2014"
        _state.zone_edid[out] = edid_str
        _dset(f"zone_edid_{out}", edid_str)

    for src in range(1, _state.n_inputs + 1):
        gv = await _try(m.query_source_edid_group(src))
        edid_str = ("Member" if gv else "Not member") if gv is not None else "\u2014"
        _state.source_edid[src] = edid_str
        _dset(f"source_edid_{src}", edid_str)

    _set_status_ok("Diagnostics refreshed")


async def _do_connect(host: str, port: int, model: str) -> None:
    if not host:
        _set_status_err("Please enter a host address.")
        _ui_call(_set_busy, False)
        return

    info = MODELS[model]
    _state.n_outputs = info["video_outputs"]
    _state.n_inputs = info["video_inputs"]
    _state.n_audio = info["audio_outputs"] or info["video_outputs"]
    _state.n_hdbt = info.get("hdbt_outputs", 0)

    _set_status(f"Connecting to {host}:{port}…")
    try:
        m = LeafMatrix(host, port, model, keepalive_interval=30, timeout=5)
        m.on_input_change(_on_input_change)
        m.on_volume_change(_on_volume_change)
        m.on_mute_change(_on_mute_change)
        await m.connect()
        _state.matrix = m
    except (LeafAVError, OSError) as exc:
        _set_status_err(f"Connection failed: {exc}")
        _ui_call(_set_busy, False)
        return

    _ui_call(_build_routing_grid, _state.n_outputs, _state.n_inputs)
    _ui_call(_build_audio_controls, _state.n_audio)
    _ui_call(_build_diagnostics_panel, _state.n_hdbt, _state.n_outputs, _state.n_inputs)
    _ui_call(_set_connected_ui, True)
    _set_status_ok(f"Connected to {host}:{port} ({model})")

    await _do_refresh_all()
    await _do_refresh_diagnostics()


async def _do_disconnect() -> None:
    if _state.matrix:
        await _state.matrix.disconnect()
        _state.matrix = None
    _reset_grid_cells()
    _ui_call(_set_connected_ui, False)
    _set_status("Disconnected")


# ---------------------------------------------------------------------------
# GUI callbacks (called from DPG on the main thread)
# ---------------------------------------------------------------------------

def _on_connect_btn() -> None:
    host = dpg.get_value("inp_host").strip()
    port = int(dpg.get_value("inp_port"))
    model = dpg.get_value("inp_model")
    _set_busy(True)
    _submit(_do_connect(host, port, model))


def _on_disconnect_btn() -> None:
    _set_busy(True)
    _submit(_do_disconnect())


def _on_refresh_btn() -> None:
    if not _state.matrix:
        return
    _submit(_do_refresh_all())


def _on_refresh_diag_btn() -> None:
    if not _state.matrix:
        return
    _submit(_do_refresh_diagnostics())


def _on_cell_click(sender, app_data, user_data) -> None:
    output, input_ = user_data
    # Give immediate amber feedback on the main thread before awaiting the network
    _set_cell_pending_now(output, input_)
    _submit(_do_switch(output, input_))


def _on_volume_slider(sender, app_data, user_data) -> None:
    _submit(_do_set_volume(user_data, int(app_data)))


def _on_mute_checkbox(sender, app_data, user_data) -> None:
    _submit(_do_toggle_mute(user_data, bool(app_data)))


def _set_item_value(tag: str, value: Any) -> None:
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, value)


# ---------------------------------------------------------------------------
# Dynamic widget builders (called from asyncio thread after connect)
# ---------------------------------------------------------------------------

def _build_routing_grid(n_outputs: int, n_inputs: int) -> None:
    """Replace the routing tab content with a fresh output/input table."""
    if dpg.does_item_exist("routing_content"):
        dpg.delete_item("routing_content", children_only=True)
    if dpg.does_item_exist("routing_hint"):
        dpg.configure_item("routing_hint", show=False)

    with dpg.table(
        parent="routing_content",
        tag="routing_table",
        header_row=True,
        borders_innerH=True,
        borders_innerV=True,
        borders_outerH=True,
        borders_outerV=True,
        row_background=True,
        resizable=True,
        policy=dpg.mvTable_SizingFixedFit,
        scrollY=True,
        height=-1,
    ):
        dpg.add_table_column(label="Output", width_fixed=True, init_width_or_weight=78)
        dpg.add_table_column(label="Current", width_fixed=True, init_width_or_weight=82)
        for inp in range(1, n_inputs + 1):
            dpg.add_table_column(label=f"In {inp}", width_fixed=True, init_width_or_weight=54)

        for out in range(1, n_outputs + 1):
            with dpg.table_row():
                dpg.add_text(f"Out {out}")
                dpg.add_text("—", tag=_route_label_tag(out), color=(180, 180, 180, 255))
                for inp in range(1, n_inputs + 1):
                    dpg.add_button(
                        label="○",
                        tag=_cell_tag(out, inp),
                        callback=_on_cell_click,
                        user_data=(out, inp),
                        width=42,
                        height=26,
                    )


def _build_audio_controls(n_audio_outputs: int) -> None:
    """Replace the audio tab content with per-output volume/mute rows."""
    if dpg.does_item_exist("audio_content"):
        dpg.delete_item("audio_content", children_only=True)
    if dpg.does_item_exist("audio_hint"):
        dpg.configure_item("audio_hint", show=False)

    if n_audio_outputs == 0:
        with dpg.group(parent="audio_content"):
            dpg.add_text("This model has no analogue audio outputs.")
        return

    with dpg.group(parent="audio_content"):
        with dpg.table(header_row=True, borders_innerH=True, borders_outerH=True,
                       borders_outerV=True, resizable=True,
                       tag="audio_table"):
            dpg.add_table_column(label="Output", width_fixed=True, init_width_or_weight=80)
            dpg.add_table_column(label="Volume (0–100)")
            dpg.add_table_column(label="Mute", width_fixed=True, init_width_or_weight=70)

            for out in range(1, n_audio_outputs + 1):
                with dpg.table_row():
                    dpg.add_text(f"Out {out}")
                    dpg.add_slider_int(
                        tag=_vol_tag(out),
                        default_value=_state.volumes.get(out, 50),
                        min_value=0,
                        max_value=100,
                        width=-1,
                        callback=_on_volume_slider,
                        user_data=out,
                    )
                    dpg.add_checkbox(
                        tag=_mute_tag(out),
                        label="",
                        default_value=_state.mutes.get(out, False),
                        callback=_on_mute_checkbox,
                        user_data=out,
                    )


def _build_diagnostics_panel(n_hdbt: int, n_outputs: int, n_inputs: int) -> None:
    """Build (or rebuild) the Diagnostics tab content after connecting."""
    if dpg.does_item_exist("diag_content"):
        dpg.delete_item("diag_content", children_only=True)
    if dpg.does_item_exist("diag_hint"):
        dpg.configure_item("diag_hint", show=False)

    p = "diag_content"

    # ── Device Info ───────────────────────────────────────────────────────
    with dpg.collapsing_header(label="Device Info", parent=p, default_open=True):
        with dpg.table(header_row=False, borders_innerH=True, borders_outerH=True,
                       borders_outerV=True):
            dpg.add_table_column(label="Field", width_fixed=True, init_width_or_weight=200)
            dpg.add_table_column(label="Value")
            for key, label in [
                ("fw_version",        "Firmware version"),
                ("fw_server_version", "Server firmware version"),
                ("compat_mode",       "Compatibility mode"),
                ("edid_group_mode",   "Advanced EDID group mode"),
                ("update_status",     "Firmware update status"),
            ]:
                with dpg.table_row():
                    dpg.add_text(label)
                    dpg.add_text("\u2014", tag=_diag_tag(key), color=(180, 180, 180, 255))

    # ── HDBaseT Status ────────────────────────────────────────────────────
    if n_hdbt > 0:
        with dpg.collapsing_header(label="HDBaseT Status", parent=p, default_open=True):
            with dpg.table(
                header_row=True,
                borders_innerH=True, borders_innerV=True,
                borders_outerH=True, borders_outerV=True,
                row_background=True,
                resizable=True,
            ):
                dpg.add_table_column(label="Out",          width_fixed=True, init_width_or_weight=55)
                dpg.add_table_column(label="Cable Length", width_fixed=True, init_width_or_weight=110)
                dpg.add_table_column(label="Link Status",  width_fixed=True, init_width_or_weight=160)
                dpg.add_table_column(label="IR Config")
                for out in range(1, n_hdbt + 1):
                    with dpg.table_row():
                        dpg.add_text(f"Out {out}")
                        dpg.add_text("\u2014", tag=_diag_tag(f"hdbt_cable_{out}"), color=(180, 180, 180, 255))
                        dpg.add_text("\u2014", tag=_diag_tag(f"hdbt_link_{out}"),  color=(180, 180, 180, 255))
                        dpg.add_text("\u2014", tag=_diag_tag(f"ir_status_{out}"),  color=(180, 180, 180, 255))

    # ── Advanced EDID Group Membership ────────────────────────────────────
    with dpg.collapsing_header(label="Advanced EDID Group Membership",
                               parent=p, default_open=False):
        dpg.add_text("Zones (outputs):")
        with dpg.table(
            header_row=True,
            borders_innerH=True, borders_innerV=True,
            borders_outerH=True, borders_outerV=True,
            row_background=True,
        ):
            dpg.add_table_column(label="Output", width_fixed=True, init_width_or_weight=80)
            dpg.add_table_column(label="EDID Group Member")
            for out in range(1, n_outputs + 1):
                with dpg.table_row():
                    dpg.add_text(f"Out {out}")
                    dpg.add_text("\u2014", tag=_diag_tag(f"zone_edid_{out}"), color=(180, 180, 180, 255))

        dpg.add_spacer(height=6)
        dpg.add_text("Sources (inputs):")
        with dpg.table(
            header_row=True,
            borders_innerH=True, borders_innerV=True,
            borders_outerH=True, borders_outerV=True,
            row_background=True,
        ):
            dpg.add_table_column(label="Input", width_fixed=True, init_width_or_weight=80)
            dpg.add_table_column(label="EDID Group Member")
            for src in range(1, n_inputs + 1):
                with dpg.table_row():
                    dpg.add_text(f"In {src}")
                    dpg.add_text("\u2014", tag=_diag_tag(f"source_edid_{src}"), color=(180, 180, 180, 255))


# ---------------------------------------------------------------------------
# Main window layout
# ---------------------------------------------------------------------------

def _build_ui() -> None:
    _create_themes()

    with dpg.window(label="pyLeafAV", tag="primary_window", no_close=True):

        # ── Connection bar ────────────────────────────────────────────────
        with dpg.group(horizontal=True):
            dpg.add_text("Host")
            dpg.add_input_text(tag="inp_host", hint="192.168.x.x",
                               width=160, no_spaces=True)
            dpg.add_text("Port")
            dpg.add_input_int(tag="inp_port", default_value=8105,
                              min_value=1, max_value=65535, width=80,
                              min_clamped=True, max_clamped=True)
            dpg.add_text("Model")
            dpg.add_combo(tag="inp_model", items=list(MODELS),
                          default_value="LU862", width=110)
            dpg.add_button(tag="btn_connect", label="Connect",
                           callback=_on_connect_btn)
            dpg.add_button(tag="btn_refresh", label="Refresh",
                           callback=_on_refresh_btn)
            dpg.disable_item("btn_refresh")

        dpg.add_spacer(height=4)
        dpg.add_text("Not connected", tag="status_bar",
                     color=(180, 180, 180, 255))
        dpg.add_separator()

        # ── Tabs ──────────────────────────────────────────────────────────
        with dpg.tab_bar(tag="tabs"):

            with dpg.tab(label="Routing Matrix"):
                dpg.add_text(
                    "Connect to a device to see the routing matrix.",
                    tag="routing_hint",
                )
                with dpg.group(tag="routing_content"):
                    pass

            with dpg.tab(label="Audio Controls"):
                dpg.add_text(
                    "Connect to a device to see audio controls.",
                    tag="audio_hint",
                )
                with dpg.group(tag="audio_content"):
                    pass

            with dpg.tab(label="Diagnostics"):
                with dpg.group(horizontal=True):
                    dpg.add_text("HDBaseT, EDID, firmware, IR status.")
                    dpg.add_spacer(width=12)
                    dpg.add_button(tag="btn_refresh_diag", label="Refresh Diagnostics",
                                   callback=_on_refresh_diag_btn)
                    dpg.disable_item("btn_refresh_diag")
                dpg.add_separator()
                dpg.add_text(
                    "Connect to a device to see diagnostics.",
                    tag="diag_hint",
                )
                with dpg.group(tag="diag_content"):
                    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    level = logging.DEBUG if os.getenv("LEAFAV_DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s: %(message)s",
    )

    dpg.create_context()
    dpg.create_viewport(
        title="pyLeafAV \u2014 Leaf HDMI Matrix Control",
        width=1100,
        height=680,
        min_width=700,
        min_height=420,
    )
    dpg.setup_dearpygui()

    _build_ui()

    dpg.set_primary_window("primary_window", True)
    dpg.show_viewport()

    while dpg.is_dearpygui_running():
        _drain_ui_queue()
        dpg.render_dearpygui_frame()

    _drain_ui_queue()

    dpg.destroy_context()
    _loop.call_soon_threadsafe(_loop.stop)


if __name__ == "__main__":
    main()
