"""
Command-line interface for Leaf HDMI matrix switches.

Usage examples::

    leafav --host 192.168.1.10 status
    leafav --host 192.168.1.10 switch 1 2
    leafav --host 192.168.1.10 --model LU1082 switch 3 6
    leafav --host 192.168.1.10 mute 1
    leafav --host 192.168.1.10 volume 2 75
    leafav --host 192.168.1.10 volume-up 1
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from . import protocol as proto
from .client import LeafMatrix, LeafAVError
from .protocol import MODELS


# ---------------------------------------------------------------------------
# Shared argument helpers
# ---------------------------------------------------------------------------

def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", required=True, metavar="HOST",
                        help="IP address of the matrix switch")
    parser.add_argument("--port", type=int, default=8105, metavar="PORT",
                        help="TCP port (default: 8105)")
    parser.add_argument("--model", default="LU862",
                        choices=list(MODELS), metavar="MODEL",
                        help=f"Matrix model; one of {list(MODELS)} (default: LU862)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging (show raw bytes sent/received)")


def _make_matrix(args: argparse.Namespace) -> LeafMatrix:
    return LeafMatrix(args.host, args.port, args.model)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

async def _handle_switch(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        await m.switch_input(args.output, args.input)
    print(f"Output {args.output} → Input {args.input}")


async def _handle_disconnect(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        await m.disconnect_av(args.output)
    print(f"Output {args.output} disconnected")


async def _handle_mute(args: argparse.Namespace) -> None:
    muting: bool = args.subcommand == "mute"
    async with _make_matrix(args) as m:
        if muting:
            await m.mute(args.output)
        else:
            await m.unmute(args.output)
    print(f"Output {args.output} {'muted' if muting else 'unmuted'}")


async def _handle_volume(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        await m.set_volume(args.output, args.level)
    print(f"Output {args.output} volume set to {args.level}")


async def _handle_volume_pulse(args: argparse.Namespace) -> None:
    going_up: bool = args.subcommand == "volume-up"
    async with _make_matrix(args) as m:
        if going_up:
            await m.volume_up(args.output)
        else:
            await m.volume_down(args.output)
    print(f"Output {args.output} volume {'up' if going_up else 'down'}")


async def _handle_bass(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        await m.set_bass(args.output, args.level)
    print(f"Output {args.output} bass set to {args.level}")


async def _handle_treble(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        await m.set_treble(args.output, args.level)
    print(f"Output {args.output} treble set to {args.level}")


async def _handle_status(args: argparse.Namespace) -> None:
    async with _make_matrix(args) as m:
        routing = await m.get_routing()

    col_w = 8
    header = f"{'Output':<{col_w}} {'Input':<{col_w}}"
    print()
    print(header)
    print("-" * len(header))
    for out in sorted(routing):
        inp = routing.get(out)
        label = f"In {inp}" if inp else "—"
        print(f"{'Out ' + str(out):<{col_w}} {label:<{col_w}}")
    print()


def _mid_level(raw: int) -> int:
    """Map raw 31–225 tone/balance bytes to an approximate 0–100 level."""
    return round(max(0, min(194, raw - 31)) / 194 * 100)


def _input_label(value: int) -> str:
    return "Disconnected" if value == 0 else f"In {value}"


async def _try_query(coro: Any) -> Any:
    try:
        return await coro
    except Exception as exc:
        return f"ERROR: {exc}"


async def _handle_dump(args: argparse.Namespace) -> None:
    """Print all read-only data points exposed by the Control4 driver."""
    async with _make_matrix(args) as m:
        print("Ping/handshake: OK (LEAF[*])")
        print(f"Device: {args.host}:{args.port} ({args.model})")

        print("\nAV routing")
        for out in range(1, m.video_outputs + 1):
            value = await _try_query(m.query_av_source(out))
            label = _input_label(value) if isinstance(value, int) else value
            print(f"  Out {out}: {label}")

        print("\nAV locks")
        for out in range(1, m.video_outputs + 1):
            value = await _try_query(m.query_av_lock(out))
            if isinstance(value, int):
                label = "Not locked/off" if value == 0 else f"Locked to In {value}"
            else:
                label = value
            print(f"  Out {out}: {label}")

        audio_outputs = m.audio_outputs or m.video_outputs
        print("\nAudio source/settings")
        for out in range(1, audio_outputs + 1):
            src = await _try_query(m.query_audio_source(out))
            vol = await _try_query(m.query_volume(out))
            bass = await _try_query(m.query_bass(out))
            treble = await _try_query(m.query_treble(out))
            balance = await _try_query(m.query_balance(out))
            muted = await _try_query(m.query_mute(out))
            delay = await _try_query(m.query_delay(out))
            parts = [
                f"src={_input_label(src) if isinstance(src, int) else src}",
                f"vol={vol}%" if isinstance(vol, int) else f"vol={vol}",
                (
                    f"bass={_mid_level(bass)}% raw 0x{bass:02X}"
                    if isinstance(bass, int) else f"bass={bass}"
                ),
                (
                    f"treble={_mid_level(treble)}% raw 0x{treble:02X}"
                    if isinstance(treble, int) else f"treble={treble}"
                ),
                (
                    f"balance={_mid_level(balance)}% raw 0x{balance:02X}"
                    if isinstance(balance, int) else f"balance={balance}"
                ),
                (
                    f"mute={'muted' if muted else 'unmuted'}"
                    if isinstance(muted, bool) else f"mute={muted}"
                ),
                f"delay={delay} ms" if isinstance(delay, int) else f"delay={delay}",
            ]
            print(f"  Out {out}: " + "; ".join(parts))

        status_labels = {
            0x00: ("power/status", lambda v: f"raw {v} (driver treats as always on)"),
            0x10: ("firmware major", lambda v: f"{v:x}"),
            0x11: ("firmware minor", lambda v: f"{v:x}"),
            0x14: ("server firmware major", lambda v: f"{v:x}"),
            0x15: ("server firmware minor", lambda v: f"{v:x}"),
            0x30: ("compatibility mode", lambda v: {0: "disabled/off", 1: "enabled/on"}.get(v, f"raw {v}")),
            0x62: ("calibration state", lambda v: {0: "not calibrated", 1: "calibrated", 0xFF: "calibration failed"}.get(v, f"raw {v}")),
            0x63: ("calibration activity", lambda v: {0: "not calibrating", 1: "calibrating"}.get(v, f"raw {v}")),
            0x68: ("group mode", lambda v: {0: "off / normal EDID", 1: "active fixed EDID", 2: "active calibration EDID"}.get(v, f"raw {v}")),
            0xB9: ("firmware update availability", lambda v: {0: "no file available", 1: "no update available", 2: "update available", 3: "update triggered"}.get(v, f"raw {v}")),
            0xBA: ("last firmware update status", lambda v: {0: "no status", 1: "success", 2: "failed"}.get(v, f"raw {v}")),
        }
        status_values: dict[int, Any] = {}
        print("\nGeneral status")
        for subcode, (label, formatter) in status_labels.items():
            value = await _try_query(m.query_status(subcode))
            status_values[subcode] = value
            text = formatter(value) if isinstance(value, int) else value
            print(f"  {label}: {text}")
        if isinstance(status_values.get(0x10), int) and isinstance(status_values.get(0x11), int):
            print(f"  firmware combined: {status_values[0x10]:x}.{status_values[0x11]:x}")
        if isinstance(status_values.get(0x14), int) and isinstance(status_values.get(0x15), int):
            print(f"  server firmware combined: {status_values[0x14]:x}.{status_values[0x15]:x}")

        print("\nURL update string")
        chars: list[str] = []
        for _ in range(256):
            value = await _try_query(
                m.query_packet(
                    proto.CMD_STATUS,
                    proto.RESP_STATUS,
                    0xB8,
                    param=0xB8,
                    response_zone_byte=0xB8,
                )
            )
            if not isinstance(value, tuple):
                chars = [str(value)]
                break
            byte_value, _ = value
            if byte_value == 0x00:
                break
            chars.append(chr(byte_value) if 32 <= byte_value < 127 else f"\\x{byte_value:02x}")
        print("  " + ("".join(chars) if chars else "empty"))

        hdbt_outputs = 6 if args.model in {"LU862", "LU862D"} else 0
        if hdbt_outputs:
            hdbt_link = {
                0: "no HDBaseT link",
                1: "good HDBaseT link",
                2: "low power mode",
                3: "Ethernet only mode",
                4: "link quality warning",
            }
            print("\nHDBaseT status")
            for out in range(1, hdbt_outputs + 1):
                zone = proto.output_to_byte(out)
                length = await _try_query(m.query_packet(0x70, 0x71, zone))
                link = await _try_query(m.query_packet(0x72, 0x73, zone))
                if isinstance(length, tuple):
                    length_text = "less than 20 m" if length[0] < 20 else f"approx {length[0]} m"
                else:
                    length_text = str(length)
                if isinstance(link, tuple):
                    link_text = hdbt_link.get(link[0], f"raw {link[0]}")
                else:
                    link_text = str(link)
                print(f"  Zone {out}: cable={length_text}; link={link_text}")

            ir_status = {
                2: "main unit IR transmitter bug (2-pin)",
                3: "main unit direct drive input",
                4: "main unit IR detector (3-pin)",
                5: "main unit IR detector (older breakout)",
            }
            print("\nIR status")
            for out in range(1, hdbt_outputs + 1):
                zone = proto.output_to_byte(out)
                value = await _try_query(m.query_packet(0x74, 0x75, zone))
                if isinstance(value, tuple):
                    text = ir_status.get(value[0], f"raw {value[0]}")
                else:
                    text = str(value)
                print(f"  Port {out}: {text}")

        print("\nAdvanced EDID group membership")
        print("  Zones:")
        for out in range(1, m.video_outputs + 1):
            zone = proto.output_to_byte(out)
            value = await _try_query(m.query_packet(0x78, 0x79, zone))
            if isinstance(value, tuple):
                text = {0: "not member", 1: "member"}.get(value[0], f"raw {value[0]}")
            else:
                text = str(value)
            print(f"    Zone {out}: {text}")
        print("  Sources:")
        for source in range(1, m.video_inputs + 1):
            value = await _try_query(
                m.query_packet(0x7A, 0x7B, 0x00, param=source, response_zone_byte=-1)
            )
            if isinstance(value, tuple):
                source_byte, member = value
                if source_byte == source:
                    text = {0: "not member", 1: "member"}.get(member, f"raw {member}")
                else:
                    text = f"unexpected source byte {source_byte}, member {member}"
            else:
                text = str(value)
            print(f"    Source {source}: {text}")


async def _handle_keepalive(args: argparse.Namespace) -> None:
    from .protocol import CMD_KEEP_ALIVE
    m = _make_matrix(args)
    await m.connect()
    await m._raw_send(CMD_KEEP_ALIVE)
    print("Keepalive sent")
    await m.disconnect()


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leafav",
        description="Control a Leaf HDMI matrix switch over TCP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  leafav --host 192.168.1.10 status
  leafav --host 192.168.1.10 switch 1 2
  leafav --host 192.168.1.10 mute 3
  leafav --host 192.168.1.10 volume 1 75
  leafav --host 192.168.1.10 volume-up 2
  leafav --host 192.168.1.10 bass 1 128
    leafav --host 192.168.1.10 dump
""",
    )
    _add_connection_args(parser)
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # switch OUTPUT INPUT
    p = sub.add_parser("switch", help="Route an input to an output")
    p.add_argument("output", type=int, metavar="OUTPUT",
                   help="Output number (1-indexed)")
    p.add_argument("input", type=int, metavar="INPUT",
                   help="Input number (1-indexed)")

    # disconnect OUTPUT
    p = sub.add_parser("disconnect", help="Disconnect (blank) an output")
    p.add_argument("output", type=int, metavar="OUTPUT")

    # mute OUTPUT / unmute OUTPUT
    for name in ("mute", "unmute"):
        p = sub.add_parser(name, help=f"{name.capitalize()} an output")
        p.add_argument("output", type=int, metavar="OUTPUT")

    # volume OUTPUT LEVEL
    p = sub.add_parser("volume", help="Set volume on an output (0–100)")
    p.add_argument("output", type=int, metavar="OUTPUT")
    p.add_argument("level", type=int, metavar="LEVEL",
                   help="Volume level 0–100")

    # volume-up / volume-down
    for name in ("volume-up", "volume-down"):
        p = sub.add_parser(name, help=f"Pulse volume {name.split('-')[1]} on an output")
        p.add_argument("output", type=int, metavar="OUTPUT")

    # bass OUTPUT LEVEL
    p = sub.add_parser("bass", help="Set bass level on an output (31–225)")
    p.add_argument("output", type=int, metavar="OUTPUT")
    p.add_argument("level", type=int, metavar="LEVEL")

    # treble OUTPUT LEVEL
    p = sub.add_parser("treble", help="Set treble level on an output (31–225)")
    p.add_argument("output", type=int, metavar="OUTPUT")
    p.add_argument("level", type=int, metavar="LEVEL")

    # status
    sub.add_parser("status", help="Query and print the full routing table")

    # dump
    sub.add_parser("dump", help="Query all read-only diagnostics exposed by the driver")

    # keepalive
    sub.add_parser("keepalive", help="Send a single keepalive packet (useful for testing)")

    return parser


# ---------------------------------------------------------------------------
# Subcommand dispatch table
# ---------------------------------------------------------------------------

_HANDLERS: dict[str, Any] = {
    "switch":       _handle_switch,
    "disconnect":   _handle_disconnect,
    "mute":         _handle_mute,
    "unmute":       _handle_mute,
    "volume":       _handle_volume,
    "volume-up":    _handle_volume_pulse,
    "volume-down":  _handle_volume_pulse,
    "bass":         _handle_bass,
    "treble":       _handle_treble,
    "status":       _handle_status,
    "dump":         _handle_dump,
    "keepalive":    _handle_keepalive,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    level = logging.DEBUG if (args.verbose or os.getenv("LEAFAV_DEBUG")) else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(levelname)s:%(name)s: %(message)s",
    )

    handler = _HANDLERS.get(args.subcommand)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        asyncio.run(handler(args))
    except LeafAVError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
