# egolf-battery-report

A small Python script (`get_battery.py`) that reads diagnostic data from
a **2019 VW e-Golf** over OBD-II using a vLinker (ELM327-compatible)
USB adapter, and prints a summary of pack state and battery health.

## What it reads

From the **Battery Energy Control Module (BMS, J840)** at request
`0x7E5` / response `0x7ED`:

- **Gross State of Charge** — DID `028C`. The internal SoC, before the
  dash's bottom/top display buffer.
- **Pack voltage** — DID `1E3B`.
- **Pack current** — DID `1E3D`. Sign convention: positive = charge,
  negative = discharge (per the EVNotify convention; verify on your car).
- **Min / max cell voltage and pack spread** — DIDs `1E33` and `1E34`.
  Requires the BMS to be in extended diagnostic session (`10 03`); the
  script opens and closes the session automatically.
- **Per-cell voltages** (optional, via `--cells`) — DIDs `1E40` through
  `1E97`, one query per cell, 88 cells total. Adds ~5 s to the run.
  **Currently produces wrong values — see Future Work below.**

From the **gateway (control unit 0x19)** over plain ISO-TP at request
`0x710` / response `0x77A`, with explicit `ATCRA` and manual flow-control
configuration:

- **Maximum energy content** — DID `2AB2`. The first u16 BE × 2 = Wh
  (i.e. raw value is in 0.5 Wh units). On a healthy 2019 pack this
  lands at ~31.75 kWh, matching the documented usable capacity.
- **Pack current cross-check** — DID `2AB6`. Uses the same
  `(raw - 2044) / 4` encoding as BMS DID `1E3D`; useful sanity that
  gateway and BMS agree.
- **Cell-module temperatures** — DID `2AB7`. 7 sensors in
  deciCelsius (`raw / 10 = °C`), plus 7 placeholder slots that always
  read 5.0 °C on this car.

Derived from the above:

- **State of Health** = `2AB2 max energy / 35.8 kWh` (the e-Golf's
  nominal gross capacity, per EVNotify).
- **Current usable energy** = `SoC × max energy from 2AB2`. There is no
  single gateway DID that stores current energy as a flat Wh value
  (probably because VW computes it in the display layer from
  `SoC × max`), so the derivation is the right answer rather than a
  workaround.

## Why this exists

Commercial tools (VCDS, OBDeleven, ODIS-E) can read the same data, but
they are Windows-only and/or paid. This script was an exercise in doing
the same diagnostic readout with an open-source toolchain — `pyserial`
talking to a generic ELM327 over USB, no proprietary stack — and in
understanding *why* each layer is needed (UDS service numbering,
ISO-TP framing, ELM327 quirks, VAG's split between BMS-side and
gateway-side data).

The result is a single self-contained script that produces a complete
pack-health snapshot in ~3 seconds (~8 with `--cells`).

## Usage

Dependencies:

```bash
pip install pyserial
```

Plug a vLinker (or any ELM327-compatible USB adapter) into the OBD-II
port, then:

```bash
# Standard run: BMS pack-level + cell balance + gateway summary
python3 get_battery.py

# Same plus full 88-cell voltage sweep
python3 get_battery.py --cells
```

The script assumes the adapter is at `/dev/ttyUSB0`, 115200 baud. Adjust
the constants at the top of `get_battery.py` if your setup differs.

The car must be powered on (ignition on or "ready") for the BMS to
answer.

## How it was created

This started as a broken stub that tried to read DIDs `2A09` and `2A0A`
against the gateway and printed nothing useful. It was rebuilt
iteratively over a single working session, with each step driven by an
actual response from the car. The git history captures the trail:

1. **Framing**: replaced fixed-delay `read_all()` with a loop that reads
   until the ELM327 `>` ready prompt arrives, fixing the off-by-one
   response bleed that caused the first query to look empty.
2. **Addressing**: switched the header from the gateway (`0x7E0`) to the
   BMS (`0x7E5`), added a VIN sanity-check via DID `F190`, and
   discovered that the original DIDs were MEB-platform identifiers
   (ID.3/ID.4) — never valid for the MK7 e-Golf.
3. **Correct BMS DIDs**: replaced the broken queries with `028C` (gross
   SoC), `1E3B` (pack voltage), and `1E3D` (pack current), each
   decoded inline.
4. **Gateway probe**: pointed the chip at `0x710 / 0x77A` and added
   explicit ISO-TP flow control (`ATFCSH` / `ATFCSD` / `ATFCSM`) so the
   gateway's multi-frame responses would complete. Discovered DID
   `2AB2` (max energy, → SoH) and ruled out DID `2AB8` (mislabeled as
   "current energy" in community sources).
5. **DID-neighborhood scan**: a sweep of `2AB0..2ABF` confirmed that no
   gateway DID holds "current energy" as a flat value, but uncovered
   `2AB6` (gateway-side pack current — cross-check for the BMS reading)
   and `2AB7` (cell-module temperatures).
6. **Parser hardening**: when multi-frame responses included trailing
   CAN-frame padding (`AA AA`), the parser was treating the padding as
   data. Fixed by reading the ISO-TP First-Frame length header and
   truncating to it.
7. **Cell voltages**: added per-cell readout from DIDs `1E40..1E97`,
   gated behind UDS extended diagnostic session (`10 03`). Always
   reads `1E33` / `1E34` for the cheap balance check; `--cells` enables
   the full 88-cell sweep.

Most of the dead ends along the way (wrong DIDs, missing flow control,
truncated multi-frame responses, NRCs from default-session queries) are
captured as commit messages with "why" explanations, in case any of
this is useful for adapting the script to a related car (e-Up, ID.3).

The work was done collaboratively with Claude (Anthropic's coding
assistant); the iterative debugging — interpreting raw OBD responses,
identifying off-by-one framing bugs, distinguishing platform-specific
DIDs, deciding when to research community sources vs. probe the bus
directly — was largely driven by that conversation.

## References

Reverse-engineered from a mix of community projects and one academic
paper. The most useful sources:

- **TUM-FTM e-Golf paper**:
  Merkle, Pöthig, Schmid, *"Estimate e-Golf Battery State Using
  Diagnostic Data and a Digital Twin,"* MDPI Batteries 2021, 7(1):15.
  <https://www.mdpi.com/2313-0105/7/1/15>
  — Most authoritative source for the cell-voltage DID family and the
  extended-session requirement.
- **EVNotify e-Golf module** (open-source EV telemetry app):
  <https://github.com/EVNotify/EVNotify/blob/master/app/www/components/cars/E_GOLF.vue>
  — Source for DID `028C` (SoC), `1E3B` (V), `1E3D` (I), and the
  `35.8 kWh` nominal pack constant.
- **OVMS3 e-Up driver** (Open Vehicle Monitoring System, closest cousin
  to the e-Golf BMS):
  <https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3/tree/master/vehicle/OVMS.V3/components/vehicle_vweup/src>
  — Reference implementation for cell-voltage polling (DIDs `1E40+`)
  and the `value / 4096` scaling.
- **obd-amigos PID database** (via meatpiHQ/wican-fw):
  <https://github.com/meatpiHQ/wican-fw/issues/168>
  — Confirms BMS at `7E5 / 7ED` is control unit `8C` and lists DID
  `028C` for SoC.
- **iternio ev-obd-pids**:
  <https://github.com/iternio/ev-obd-pids/blob/main/volkswagen/eGolf.json>
  — Pack-level PID catalog; no cell DIDs but useful cross-check.
- **GoingElectric VAG OBD-2 wiki** (German):
  <https://www.goingelectric.de/wiki/Liste-der-OBD2-Codes/>
  — Confirms VAG control-unit-to-CAN-ID mappings.

Specifications:

- **ISO 15765-2 (ISO-TP)**: the transport-layer framing the script
  reassembles (First Frame length header, Consecutive Frames, Flow
  Control).
- **ISO 14229 (UDS)**: the application-layer protocol used for the
  `0x22` ReadDataByIdentifier and `0x10` DiagnosticSessionControl
  requests.
- **ELM327 datasheet (v2.3)**: AT command set, automatic vs. manual
  flow control, response filtering.

## Future Work

### Known broken: per-cell voltage sweep

**Pick this up first when resuming.**

The `--cells` sweep currently returns ~0.758 V per cell, which is wrong.
At the same time, the aggregate min/max DIDs (`1E33` / `1E34`) return
correct values (~4.1 V at high SoC).

Diagnostic evidence already gathered:

- Decoded `0.758 V` from `u16 BE / 4096` means raw bytes `[0x0C, 0x20]`.
- For 4.1 V the raw should be `[0x41, 0xBA]` (as observed on `1E33`).
- The min/max responses are formatted `[VH, VL, 0x00, IDX]` — voltage
  at bytes 0..1, cell index at byte 3.
- The per-cell responses are almost certainly formatted differently,
  most likely with a leading metadata byte before the voltage (so the
  real voltage sits at bytes 1..2, not 0..1).

To resolve: temporarily log the raw bytes of the first few cell
responses, identify the actual layout, then update
`sweep_cell_voltages` to decode the correct byte offset (or scaling).
The per-cell printout in `result-03.txt` is the artifact to compare
against once a fix candidate is in hand. Cross-check by summing the
fixed per-cell values × 1 — they should land within a few volts of
`pack_voltage` from DID `1E3B` (~361 V at high SoC).

### Plan A: re-verify at meaningfully lower SoC

All measurements so far were taken near 95.6 % SoC. Re-running after
driving the pack down to ~60 % serves two purposes:

- Confirm that `2AB2` stays at ~31.75 kWh (max energy shouldn't depend
  on SoC; if it does, the scaling guess is wrong).
- Verify the BMS pack-current sign convention by capturing it during
  driving (clearly negative) or charging (clearly positive).

### True State of Health via gateway

`SoH` is currently derived from `2AB2 max energy / nominal`. A more
authoritative source would be ODIS-E's tester values for "Maximum
Energy Content" cross-referenced against the same DID. If anyone has
captured an ODIS-E session, comparing scalings would either confirm
the `× 2` interpretation or refine it.

### Other DIDs worth wiring up

- **DID `1E32`** (BMS): cumulative kWh charged / discharged over pack
  lifetime. Multi-frame, decoded as `U32 / 8583.07212` per EVNotify.
- **VWTP 2.0**: the gateway exposes additional DIDs via VWTP 2.0 only.
  Implementing a VWTP 2.0 client over the same ELM327 in raw-CAN mode
  is a multi-hundred-line undertaking; useful but overkill for the
  current scope.

### Polish

- The `/dev/ttyUSB0` path and `115200` baud rate are hard-coded; should
  move to command-line flags.
- The 88-cell sweep could be sped up via the STN-chip queued-command
  mode (vLinker MC+ uses STN2120), cutting the ~5 s sweep to ~1–2 s.
- No tests. The parser has been verified by hand against captured
  responses but there's no `pytest` harness.
