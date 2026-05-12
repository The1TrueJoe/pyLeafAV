"""
Protocol constants and utilities for Leaf HDMI matrix switches.

Wire format
-----------
Commands (host → device, TCP/network):
    ASCII wrapper: ``LEAF[XX,YY,ZZ]\n`` where XX/YY/ZZ are 2-char hex bytes.
    This matches the Control4 driver's ``PackAndQueueCommand`` +
    ``RepackCommand`` path.

Commands (host → device, serial/future use):
    3 raw binary bytes: struct.pack('BBB', cmd, param, zone_byte)

Responses (device → host):
    ASCII newline-terminated lines.
    Routing/audio responses: ``LEAF[XX,YY,ZZ]\\n``  (uppercase 2-char hex pairs)
    Keep-alive ack:          ``LEAF[*]\\n``

Zone byte conventions (on the wire):
    Output N (1-based) → wire byte = N - 1   (Out 1 → 0x00, Out 8 → 0x07)
    Input  N (1-based) → wire byte = N        (In  1 → 0x01, In  8 → 0x08)
"""
from __future__ import annotations

import re
import struct
from typing import Optional

# ---------------------------------------------------------------------------
# Command bytes (host → device)
# ---------------------------------------------------------------------------

CMD_SWITCH_AV = 0x48         # Set AV input on output (no lock)
CMD_SWITCH_AV_LOCK = 0x5D   # Set AV input on output + lock zone
CMD_SWITCH_AV_ADV = 0x6D    # Set AV input on output (advanced / override lock)
CMD_DISCONNECT_AV = 0x47    # Disconnect AV output
CMD_SWITCH_AUDIO = 0xB0     # Set audio-only input on output
CMD_DISCONNECT_AUDIO = 0xB1  # Disconnect audio output only

CMD_MUTE_ON = 0x5A
CMD_MUTE_OFF = 0x5B

CMD_VOL_UP = 0x40            # Pulse volume up (one step)
CMD_VOL_DOWN = 0x41          # Pulse volume down (one step)
CMD_SET_VOLUME = 0x59        # Set absolute volume (device scale 0x00–0xFF)

CMD_BASS_UP = 0x42
CMD_BASS_DOWN = 0x43
CMD_SET_BASS = 0x54          # param: 31–225

CMD_TREBLE_UP = 0x44
CMD_TREBLE_DOWN = 0x45
CMD_SET_TREBLE = 0x55        # param: 31–225

CMD_BALANCE_UP = 0x50
CMD_BALANCE_DOWN = 0x51
CMD_SET_BALANCE = 0x46       # param: 31–225

CMD_SET_AUDIO_DELAY = 0x63   # param: delay in ms

CMD_QUERY_AV_SOURCE = 0x34   # Query current AV input on output  → 0x35
CMD_QUERY_AV_LOCK = 0x32     # Query AV lock state               → 0x33
CMD_QUERY_AUDIO = 0x30       # Query audio input on output        → 0x31
CMD_QUERY_VOLUME = 0x20      # Query output volume               → 0x21
CMD_QUERY_BASS = 0x22        # Query output bass                 → 0x23
CMD_QUERY_TREBLE = 0x24      # Query output treble               → 0x25
CMD_QUERY_BALANCE = 0x36     # Query output balance              → 0x37
CMD_QUERY_MUTE = 0x2A        # Query output mute state           → 0x2B
CMD_QUERY_DELAY = 0x28       # Query audio delay                 → 0x29

CMD_STATUS = 0x2C            # General status query (sub-coded via param byte)
CMD_KEEP_ALIVE: bytes = b"LEAF[.]\n"

CMD_QUERY_HDBT_CABLE_LENGTH = 0x70   # HDBaseT cable length query    → 0x71
CMD_QUERY_HDBT_LINK_STATUS  = 0x72   # HDBaseT link status query     → 0x73
CMD_QUERY_IR_STATUS         = 0x74   # IR connector type query       → 0x75
CMD_QUERY_ZONE_EDID_GROUP   = 0x78   # Zone EDID group membership    → 0x79
CMD_QUERY_SOURCE_EDID_GROUP = 0x7A   # Source EDID group membership  → 0x7B

# ---------------------------------------------------------------------------
# Response byte IDs (device → host)
# ---------------------------------------------------------------------------

RESP_RETURN_AV_SOURCE = 0x35
RESP_RETURN_AV_LOCK = 0x33
RESP_RETURN_AUDIO = 0x31
RESP_RETURN_VOLUME = 0x21
RESP_RETURN_BASS = 0x23
RESP_RETURN_TREBLE = 0x25
RESP_RETURN_BALANCE = 0x37
RESP_RETURN_MUTE = 0x2B
RESP_RETURN_DELAY = 0x29

# Unsolicited change notifications (device echoes the command back)
RESP_ZONE_AV_CHANGED = 0x48      # Also 0x47 (disconnect = input 0)
RESP_ZONE_AUDIO_CHANGED = 0xB0   # Also 0xB1 (disconnect)
RESP_VOLUME_CHANGED = 0x40       # Also 0x41 (down pulse), 0x59 (set)
RESP_MUTE_CHANGED_ON = 0x5A
RESP_MUTE_CHANGED_OFF = 0x5B
RESP_ZONE_LOCK_CHANGED = 0x5D
RESP_STATUS = 0x2D
RESP_POWER = 0x0D                # Also 0x0E

RESP_HDBT_CABLE_LENGTH      = 0x71
RESP_HDBT_LINK_STATUS       = 0x73
RESP_IR_STATUS              = 0x75
RESP_ZONE_EDID_GROUP        = 0x79
RESP_SOURCE_EDID_GROUP      = 0x7B

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

MODELS: dict[str, dict[str, int]] = {
    # hdbt_outputs: number of HDBaseT output ports (0 = HDMI-only model).
    # LU862 / LU862D: 8 total outputs, first 6 are HDBaseT extender ports.
    "LU642":  {"video_inputs": 6,  "video_outputs": 6,  "audio_outputs": 8, "hdbt_outputs": 0},
    "LU642L": {"video_inputs": 6,  "video_outputs": 6,  "audio_outputs": 0, "hdbt_outputs": 0},
    "LU862":  {"video_inputs": 8,  "video_outputs": 8,  "audio_outputs": 8, "hdbt_outputs": 6},
    "LU862D": {"video_inputs": 8,  "video_outputs": 8,  "audio_outputs": 8, "hdbt_outputs": 6},
    "LU1082": {"video_inputs": 10, "video_outputs": 10, "audio_outputs": 8, "hdbt_outputs": 0},
}

# ---------------------------------------------------------------------------
# Volume mapping
# ---------------------------------------------------------------------------
# The C4 driver uses a linear mapping: C4 0-100 → device 0x00-0xFF
# (via ConvertVolumeToDevice = ProcessVolumeLevel(v, 0, 100, 0, 255)).
# The 89-entry tVolumeCurve in the Lua driver is only used for smooth
# *ramping* (stepped increments), not for direct SET_VOLUME commands.

def level_to_device(level: int) -> int:
    """Map a 0-100 volume level to the device byte (0x00–0xFF), linear."""
    return round(max(0, min(100, level)) / 100 * 255)


def device_to_level(raw: int) -> int:
    """Map a device volume byte (0x00–0xFF) back to 0–100."""
    return round(max(0, min(255, raw)) / 255 * 100)


# ---------------------------------------------------------------------------
# Zone byte conversions (1-indexed public API ↔ wire bytes)
# ---------------------------------------------------------------------------

def input_to_byte(input_number: int) -> int:
    """Input 1 → 0x01,  Input 2 → 0x02,  …"""
    return input_number


def output_to_byte(output_number: int) -> int:
    """Output 1 → 0x00,  Output 2 → 0x01,  …"""
    return output_number - 1


def byte_to_output(zone_byte: int) -> int:
    """0x00 → Output 1,  0x01 → Output 2,  …"""
    return zone_byte + 1


def byte_to_input(input_byte: int) -> int:
    """0x01 → Input 1,  0x02 → Input 2,  …"""
    return input_byte


# ---------------------------------------------------------------------------
# Packet assembly / parsing
# ---------------------------------------------------------------------------

def pack_command(cmd: int, param: int, zone_byte: int) -> bytes:
    """Assemble a raw 3-byte command packet (serial protocol format)."""
    return struct.pack("BBB", cmd, param, zone_byte)


def pack_network_command(cmd: int, param: int, zone_byte: int) -> bytes:
    """
    Assemble a TCP/network command packet.

    The Lua driver builds raw bytes with ``string.pack("bbb", ...)``, then
    ``RepackCommand`` converts each byte to uppercase hex and wraps it as
    ``LEAF[XX,YY,ZZ]\n`` before sending over TCP.
    """
    return f"LEAF[{cmd:02X},{param:02X},{zone_byte:02X}]\n".encode("ascii")


_RESPONSE_RE = re.compile(
    r"LEAF\[([0-9A-Fa-f]{2}),([0-9A-Fa-f]{2}),([0-9A-Fa-f]{2})\]"
)


def parse_response(line: str) -> Optional[tuple[int, int, int]]:
    """
    Parse a device response line.

    Returns ``(cmd_id, value, zone_byte)`` or ``None`` for keep-alive acks
    (``LEAF[*]``) or unrecognised lines.
    """
    m = _RESPONSE_RE.search(line)
    if m:
        return int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16)
    return None
