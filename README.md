# rfdtool

A Linux desktop GUI for configuring [RFDesign RFD900-series](https://store.rfdesign.com.au/) radio modems running the SiK firmware or RFDesign's **MultiComm** firmware (v4.x). Feature parity target: the official Windows-only *RFD Modem Tools*.

Supports RFD900, RFD900+, RFD900u (8051-based, Intel HEX firmware) and RFD900x, RFD900ux, RFD900x2 (STM32-based, .bin firmware).

**Developer:** [Skoll](https://skoll.dev) — [me@skoll.dev](mailto:me@skoll.dev)
**Part of:** [sUAS Tools](https://TheIT.guru/sUASTools)

![screenshot placeholder](docs/screenshot.png)

## Features

- Auto-detect connected radios across every USB serial port (FTDI / CP210x / CH340).
- Side-by-side **local + remote** S-register editor with friendly labels, validated ranges, and tooltips.
- Built-in profile presets ("MAVLink defaults", "Long range / low data rate", "Point-to-point max throughput") plus JSON profile import/export.
- Firmware update for both 8051 and STM32 radios — Intel-HEX path uses the SiK bootloader native protocol, STM32 path wraps `stm32flash`.
- Live RSSI / noise chart driven by either MAVLink `RADIO_STATUS` packets or `ATI7` polling (radio-button toggle). Polling holds the radio in command mode while the RSSI tab is open and unpaused, so the poll interval is honoured down to 0.5 s (entering command mode per poll costs ≥2.2 s); serial passthrough is paused meanwhile.
- Raw AT terminal with command history, hex-byte sends, and timestamped log.
- Startup environment checks that warn on `ModemManager` interference and missing `dialout` group membership.

## MultiComm firmware (v4.x)

RFDesign's [MultiComm firmware](https://files.rfdesign.com.au/Files/documents/MultiCommFirmwareUserManual1.0.pdf) (V2 hardware only: RFD900x2/ux2, RFD868x2/ux2) is detected automatically from the `ATI` banner or the register names in `ATI5`. It uses a completely different register map (S0–S49), and rfdtool adapts:

- **Registers in sections** (Link & RF, Network & slots, Routing, Encryption, Serial 1/2, GPIO, RC). Array registers (`ENCRYPTKEY`, `RCDEFS`, `FWDTABLE`, `PINMIRR`, `PINDEFAULT`) and the 64-bit `SLOTIDS` mask get text editors; `SLOTIDS` shows which slots it owns.
- **Ranges come from the radio.** The per-register limits it reports (e.g. `S4:NUMCHANS(N)[1..8]` on an 868 MHz unit, the allowed-function mask for each GPIO) drive the editors and validation, rather than the manual's generic table.
- **Chunked reads**: registers are read with `ATI5:x:y` because a full `ATI5` can be truncated by the radio's UART buffer.
- **No `AT&W`**: MultiComm saves on every write. *Save EEPROM* explains this; reboot to apply changes.
- **Remote node selector**: `RT` commands are broadcast to every node unless addressed (`RTI5@2`). Pick a NODEID before loading/saving the REMOTE panel in a network; "all" is fine for a pair.
- **RSSI** comes from the `AT&T=RSSI` report (MultiComm's `ATI7` is TDM timing), plotted in dBm. If no report lines appear, an `RTI` probe still tells you whether the link is up.
- **Validation** checks the manual's rules: slot size (~300–1000 bytes), channel spacing, master owns slot 1, SLOTIDS within SLOTS, subnet bounds, pin-function conflicts, RC priority, default AES key, serial port 2 pin assignment, and more.
- **Compare** knows which settings must match on every node, which are per-node, and flags NODEID / SLOTIDS collisions between two nodes. *Copy → Remote* copies only network-wide settings.
- **Presets** (Profiles → MultiComm): point-to-point auto / fixed master, RC passthrough in/out, listen-only, raw serial, flow control, the manual's 4-node network, and India 865–868 MHz at 1 W EIRP for the stock monopole and dipole antennas (read the preset notes on the Indian licence-exemption limits).

## Requirements

- Linux (tested on Ubuntu 22.04 / 24.04; should work on any modern distro)
- Python 3.10+
- `stm32flash` for RFD900x / RFD900ux firmware updates (optional; required only for STM32 flashing)

## Install (development)

```bash
git clone <this repo>
cd rfdtool
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install pyserial   # the `pyserial` import name; pulled in by requirements as well
sudo apt install stm32flash      # optional, for RFD900x/ux flashing
```

## Run

```bash
.venv/bin/python rfdtool.py
```

Useful flags:

```bash
.venv/bin/python rfdtool.py --port /dev/ttyUSB0           # auto-connect on launch
.venv/bin/python rfdtool.py --port /dev/ttyUSB0 --baud 115200
.venv/bin/python rfdtool.py --no-mavlink                  # skip RADIO_STATUS parsing
.venv/bin/python rfdtool.py --skip-checks                 # skip the startup environment checks
```

## Flash .gbl firmware from the command line (x-series)

For RFDesign x-series radios (tested on an RFD868x v2), `rfdtool.py` doubles as a headless `.gbl` flasher that replaces the manual picocom + `sx` workflow. It needs only `pyserial` and `xmodem` (no Qt):

```bash
.venv/bin/pip install pyserial xmodem
python3 rfdtool.py --port /dev/ttyUSB0 --file firmware.gbl          # asks before flashing
python3 rfdtool.py --port /dev/ttyUSB0 --file firmware.gbl --yes    # no prompts
python3 rfdtool.py --port /dev/ttyUSB0 --dry-run                    # identify only
python3 rfdtool.py --port /dev/ttyUSB0 --file firmware.gbl --batch  # radio after radio, CSV log
```

It finds the bootloader banner (`… ChipID: …`), or enters the bootloader from normal firmware with `+++` / `AT&UPDATE`. It shows the model and ChipID and asks for confirmation unless you pass `--yes`, because a file for the wrong model can brick the radio. Then it sends `UPLOAD` and transfers the file over XMODEM-CRC in 128-byte blocks with a progress bar. A failed transfer is aborted cleanly with CAN and retried, up to 3 attempts; the radio stays safely in its bootloader. On success it sends `BOOTNEW` and confirms the new firmware with `ATI`.

If the radio's normal firmware runs at another baud, add `--fw-baud N`; it's used for `+++`/`AT&UPDATE` and the post-flash check, while the bootloader stays at `--baud` (57600).

Exit codes: `0` ok · `1` bad file/arguments · `2` port missing, busy or permission denied · `3` no bootloader (force it in hardware: ground pad 9 while powering on) · `4` declined · `5` upload failed after retries · `6` flashed but the new firmware didn't answer · `130` interrupted.

## Build a standalone binary

```bash
.venv/bin/pip install pyinstaller
.venv/bin/pyinstaller rfdtool.spec
# output in dist/rfdtool/
```

## Tests

The protocol layer (AT command parsing, Intel HEX parsing, XMODEM-CRC, SiK bootloader protocol, MAVLink RADIO_STATUS extraction, STM32 flash subprocess wrapper) is covered by ~200 unit tests using a mock serial port — none of them touch real hardware.

```bash
.venv/bin/python -m pytest -v
```

## Troubleshooting

### "Permission denied: /dev/ttyUSB0"

Your user is not in the `dialout` group:

```bash
sudo usermod -aG dialout $USER
# log out and back in (or reboot) for the group change to take effect
```

`rfdtool` checks this at startup and warns if you're not a member.

### ModemManager keeps grabbing the port

If you see brief AT-command activity on the port after plugging in the radio, or commands time out the first ~10 seconds after connect, ModemManager is probing the device. Two fixes:

```bash
# Aggressive: remove ModemManager entirely (you don't need it on a desktop
# unless you have a USB cellular modem):
sudo apt remove modemmanager

# Targeted: tell ModemManager to ignore FTDI / CP210x adapters via udev:
sudo tee /etc/udev/rules.d/99-rfdtool.rules <<'EOF'
ATTRS{idVendor}=="0403", ENV{ID_MM_DEVICE_IGNORE}="1"
ATTRS{idVendor}=="10c4", ENV{ID_MM_DEVICE_IGNORE}="1"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

### Radio is "stuck in data mode" — `+++` doesn't get to command mode

Symptoms: connect succeeds (`/dev/ttyUSB0` opens) but the parameter list never loads, and the log shows `could not enter command mode`. Causes and fixes:

- **Wrong baud.** The radio remembers its serial speed across reboots. Try 115200, 38400, 9600 in turn from the Connection panel.
- **Active link saturating the UART.** If a flight controller is streaming MAVLink at high rate into the same UART pair, the +++ escape can be defeated. Disconnect the autopilot (or hold its boot button to silence it) and reconnect.
- **Wrong cable wiring.** Verify TX/RX are crossed; the breakout silk-screens are TX *from* the breakout's perspective.

### Firmware update bricked the radio (no banner, no AT response)

Hold the **BOOT** pin (also labelled CTS/IO0 on some breakouts) to ground while powering the radio on. This forces the bootloader to come up regardless of firmware state. With the BOOT pin still held:

- For 8051 radios (RFD900/+/u): re-run a known-good `.ihx` flash from the Firmware tab. The bootloader is always there even if the firmware image is corrupt.
- For STM32 radios (RFD900x/ux): re-run `stm32flash` against a known-good `.bin`. The STM32 system memory bootloader is mask-ROM and cannot be bricked.

Release the BOOT pin after the flash completes.

### `stm32flash: command not found` when flashing an RFD900x

```bash
sudo apt install stm32flash
```

The Firmware tab shows this hint automatically when you try to flash a `.bin` without `stm32flash` on PATH.

### Multiple radios on one host

`rfdtool` lists every detected serial port and highlights likely radios (FTDI / CP210x / CH340 VID/PIDs in **bold**). Pick the one you want from the dropdown and connect. Future versions will support driving more than one radio simultaneously from a single window.

## Project layout

```
rfdtool.py                 entry point
rfd/                       protocol + I/O (no Qt — fully unit-tested)
  registers.py             S-register definitions, ranges, tooltips
  multicomm.py             MultiComm register catalog, RSSI report parser, slot maths
  protocol.py              AT command builders + ATI/ATI5/ATI7 parsers (SiK + MultiComm)
  radio.py                 RadioCore (sync) + Radio (Qt wrapper, threaded)
  detector.py              port enumeration + radio fingerprinting
  ihx.py                   Intel HEX parser
  xmodem.py                XMODEM-CRC sender (sender-only)
  mavlink_parser.py        pymavlink RADIO_STATUS extractor
  uploader_8051.py         SiK 8051 bootloader driver
  uploader_stm32.py        stm32flash subprocess wrapper
  gbl_flash.py             x-series .gbl flasher CLI (bootloader UPLOAD + XMODEM-CRC)
  presets.py               built-in profiles + JSON I/O
ui/                        Qt UI layer (depends on rfd/, no reverse dep)
  main_window.py           QMainWindow assembling all tabs
  connection_panel.py      port/baud/connect/LED row
  settings_tab.py          local + remote S-register editor
  terminal_tab.py          raw AT console
  rssi_tab.py              live RSSI/noise chart
  firmware_tab.py          firmware update workflow
  system_checks.py         ModemManager / dialout startup checks
  theme.py                 forced light Fusion palette
tests/                     pytest suite for the protocol layer
```

## License

MIT — see [LICENSE](LICENSE).
