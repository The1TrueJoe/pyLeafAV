# pyLeafAV

Python library, CLI tool, and GUI for controlling **Leaf HDMI matrix switches**
over TCP (port 8105).

## Supported models

| Model    | Video inputs | Video outputs | Audio outputs | Verified |
|----------|-------------|--------------|--------------|---------|
| LU642    | 6           | 6            | 8 analogue   |         |
| LU642L   | 6           | 6            | —            |         |
| LU862    | 8           | 8            | 8 analogue   | ✅       |
| LU862D   | 8           | 8            | 8 analogue   |         |
| LU1082   | 10          | 10           | 8 analogue   |         |

All models share the same TCP protocol; only the input/output counts differ.
The LU862 is the only currently verified model — bug reports and test
results for the others are very welcome.

## Installation

```bash
pip install .           # from the repo root
# or in editable mode
pip install -e .
```

Requires Python 3.9+ and [Dear PyGui](https://github.com/hoffstadt/DearPyGui)
(installed automatically).

## CLI

```bash
# Print full routing table
leafav --host 192.168.1.10 status

# Print all read-only diagnostics exposed by the Control4 driver
leafav --host 192.168.1.10 dump

# Route Input 2 to Output 1
leafav --host 192.168.1.10 switch 1 2

# Disconnect Output 3
leafav --host 192.168.1.10 disconnect 3

# Mute / unmute Output 1
leafav --host 192.168.1.10 mute 1
leafav --host 192.168.1.10 unmute 1

# Set volume on Output 2 to 75 %
leafav --host 192.168.1.10 volume 2 75

# Pulse volume up / down
leafav --host 192.168.1.10 volume-up 1
leafav --host 192.168.1.10 volume-down 1

# Set bass / treble (device range 31–225)
leafav --host 192.168.1.10 bass   1 128
leafav --host 192.168.1.10 treble 1 128

# Use a different model or port
leafav --host 192.168.1.10 --model LU1082 --port 8105 status
```

## GUI

```bash
leafav-gui
```

Enter the host, port, and model in the connection bar and click **Connect**.

- **Routing Matrix tab** — click any cell to route that input to the output on
  that row. The active input is highlighted with a filled circle (●).
- **Audio Controls tab** — per-output volume sliders and mute checkboxes.

The GUI keeps the routing and audio displays live: unsolicited change
notifications from the device (e.g. from another controller) update the
display automatically.

## Library usage

```python
import asyncio
from leafav import LeafMatrix

async def main():
    async with LeafMatrix("192.168.1.10", model="LU862") as m:
        # Route Input 2 to Output 1
        await m.switch_input(output=1, input=2)

        # Query current routing for all outputs
        routing = await m.get_routing()
        print(routing)  # {1: 2, 2: 1, 3: 3, …}

        # Volume control
        await m.set_volume(output=1, level=80)  # 0-100
        await m.mute(output=2)
        await m.unmute(output=2)

        # Register callbacks for unsolicited device events
        m.on_input_change(lambda out, inp: print(f"Out {out} → In {inp}"))
        m.on_volume_change(lambda out, lvl: print(f"Out {out} vol={lvl}"))
        m.on_mute_change(lambda out, muted: print(f"Out {out} muted={muted}"))

asyncio.run(main())
```

## Protocol notes

- Transport: **TCP**, port **8105**
- Commands: ASCII wrapper `LEAF[XX,YY,ZZ]\n`
  - The Control4 driver creates 3 raw bytes internally, then converts those
    bytes to comma-separated uppercase hex via `RepackCommand()` before sending
    over TCP.
- Responses: ASCII lines `LEAF[XX,YY,ZZ]\n` (uppercase hex pairs)
- Keep-alive: send `LEAF[.]\n` every 30 s; device replies `LEAF[*]\n`
- Command pacing: the Control4 driver waits 250 ms between queued commands;
  pyLeafAV mirrors that default delay.
- Verified LU862 hardware closes TCP sessions after roughly six seconds even
  while keepalive acks are received. The client automatically reconnects before
  longer query batches and retries read-only queries once.
- Inputs are 1-indexed on the wire (`Input 1 → 0x01`)
- Outputs are 0-indexed on the wire (`Output 1 → 0x00`)
- Volume: linear mapping, C4 range 0-100 → device range 0x00-0xFF

The protocol was reverse-engineered from the official Control4 DriverWorks
drivers in `driver-ref/`.
