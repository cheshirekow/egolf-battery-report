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
  Encoding: `V = (u16 BE / 1000) + 1.0` — i.e. the raw value is
  millivolts above a 1.0 V baseline, *not* the `/4096` encoding the
  aggregate `1E33` / `1E34` DIDs use. This was determined empirically
  from cross-SoC captures; see *Experimental findings* below.

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
  nominal gross capacity, per EVNotify). **Treat this as a rough
  indicator only.** Two captures at different SoC produced different
  `2AB2` values (88.7 % SoH at 95.6 % SoC vs. 91.5 % SoH at 47.2 %
  SoC), so either our `× 2` scaling is wrong or `2AB2` is a dynamic
  capacity estimate rather than a static nameplate. See *Experimental
  findings* below.
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

## Experimental findings

Two full captures were taken at meaningfully different SoC to cross-
check the decode formulas and to verify which values are static
properties of the pack vs. dynamic readouts. The artifacts are
[`results-01.txt`](results-01.txt) and [`results-02.txt`](results-02.txt).

| signal | run 1 (`results-01.txt`) | run 2 (`results-02.txt`) |
|---|---|---|
| Ambient air temperature | ~80 °F (~27 °C) | ~70 °F (~21 °C) |
| Gross SoC (DID `028C`) | 95.6 % | 47.2 % |
| Pack voltage (DID `1E3B`) | 361.0 V | 324.2 V |
| Pack current (DID `1E3D`) | +0.0 A (rest) | −1.2 A (12 V keep-alive) |
| Max cell (DID `1E33`) | 4.108 V, cell #51 | 3.692 V, cell #60 |
| Min cell (DID `1E34`) | 4.099 V, **cell #32** | 3.683 V, **cell #32** |
| Cell-spread | 9.0 mV | 9.0 mV |
| Max energy (DID `2AB2` × 2) | 31.75 kWh | 32.77 kWh |
| Derived SoH | 88.7 % | 91.5 % |
| Cell-module temps (`2AB7` mean) | 14.1 °C | 13.6 °C |

### Solid: pack-level decodes

Pack voltage and SoC moved together along the expected Li-ion discharge
curve — a 48 percentage-point SoC drop produced a 36.8 V pack-voltage
drop, which works out to 0.42 V per cell over 88 cells. Cell-module
temperatures changed only ~0.5 °C despite ~6 °C ambient swing, which is
consistent with the pack's thermal mass smoothing short-term ambient
changes.

### Solid: cell-spread and weak-cell identification

`1E33` / `1E34` reported a 9.0 mV cell-to-cell spread in *both* runs.
More usefully, the **minimum cell was #32 in both runs** — the same
physical cell shows up as the laggard regardless of SoC, which is the
exact signal you want from a balance check. Worth keeping an eye on
over future captures to see whether the spread widens.

### Resolved: per-cell sweep encoding

The earlier `(raw / 4096)` decode in `sweep_cell_voltages` produced
implausible per-cell values (~0.758 V at high SoC; ~0.657 V at low
SoC). Cross-checking against the aggregate `1E33` / `1E34` actual
voltages showed a near-exact match under a different formula:

```
V_volts = (u16 BE raw / 1000) + 1.0
```

i.e. the raw value is millivolts above a 1.0 V baseline. Cross-SoC
consistency check:

| SoC | sweep avg as `/4096` | implied raw (u16 BE) | + 1000 mV | aggregate `1E33`/`1E34` actual |
|---|---|---|---|---|
| 95.6 % | 0.7582 V | 3105 | 4.105 V | 4.099 – 4.108 V |
| 47.2 % | 0.6566 V | 2690 | 3.690 V | 3.683 – 3.692 V |

The sweep's reported "spread" in raw counts (9 LSBs) also matches the
9.0 mV reported by `1E33` / `1E34`. The fix is committed; the per-cell
sweep now produces real volts.

### Unresolved: DID `2AB2` is not static

Expected `2AB2` (Maximum Energy Content) to stay constant across runs.
It didn't:

- Run 1: raw bytes `3E 02 04 00` → 15874 × 2 = **31.75 kWh**
- Run 2: raw bytes `40 02 04 00` → 16386 × 2 = **32.77 kWh**

The trailing two bytes (`04 00`) were identical, so the change is
entirely in the first u16. A 3.2 % change between two same-day
captures rules out genuine pack degradation, leaving two candidate
explanations:

1. **The `× 2` scaling is wrong.** Some other interpretation of the
   four bytes might land on a value that is truly static across runs.
2. **`2AB2` is a continuously-updated capacity estimate**, not a
   static nameplate. Many BMS implementations re-estimate pack capacity
   online based on coulomb counting and cell behavior; a few percent
   of motion between captures is normal for such a value.

The reported "State of Health" in the script's summary is therefore a
soft indicator only. Until we have a third sample (or an ODIS-E cross-
reference), we cannot distinguish "scaling wrong" from "value is
dynamic."

### Unresolved: BMS and gateway pack-current disagree

At rest (run 1) BMS `1E3D` and gateway `2AB6` agreed within the
encoding's 0.25 A quantization: BMS `+0.0 A`, gateway `+0.25 A`.

In run 2 the same two DIDs read **−1.2 A** (BMS, discharge) and
**+0.25 A** (gateway, charge) at the same moment. A ~1.5 A spread
with opposite signs is suspicious — likely a sign-convention
difference between the two ECUs, but we can't rule out a decoded-byte-
offset error on `2AB6` without further data.

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
  — Reference implementation for cell-voltage polling at DIDs
  `1E40+`. OVMS uses `value / 4096` on the e-Up, which is what the
  aggregate `1E33` / `1E34` DIDs use on the e-Golf too; the per-cell
  DIDs on the e-Golf, however, use a different `(raw / 1000) + 1.0 V`
  encoding (see *Experimental findings*). So the DID family is shared
  but the scaling is not.
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

### Nail down DID `2AB2`

The biggest open question. Two captures gave 31.75 kWh and 32.77 kWh
for what should be max energy. Three ways forward:

- **More data points.** A third capture at, say, 70 % SoC would tell
  us whether `2AB2` tracks SoC, tracks temperature, or moves
  semi-randomly between drives. The two-point dataset is not enough to
  distinguish.
- **Investigate scaling alternatives.** The trailing bytes (`04 00`)
  are constant across the two captures, so they aren't part of the
  changing value. But the leading u16 could be in some unit other
  than 0.5 Wh — anything that lands on a static value across SoC
  would be a candidate. Worth trying a few interpretations against
  the existing two raw byte samples.
- **ODIS-E cross-reference.** If anyone has access to a session
  capture from VW's official tester showing "Maximum Energy Content"
  alongside the raw `2AB2` bytes, that would settle the scaling
  immediately.

### Investigate the BMS / gateway pack-current mismatch

`1E3D` (BMS) and `2AB6` (gateway) read `−1.2 A` and `+0.25 A` in
run 2. Likely a sign-convention difference between the two ECUs, but
to confirm we need either a third capture at clearly non-zero current
or a closer look at the `2AB6` byte layout. If the gateway is using
the same `(raw - 2044) / 4` encoding but with the opposite sign
convention, flipping the sign on the gateway decode would make them
agree.

### Track cell #32 over time

Cell #32 has now been the weakest cell in both captures. Worth
checking on subsequent captures to see whether its spread vs. the
mean grows. The 88-cell sweep makes that easy now that the encoding
is fixed.

### Other DIDs worth wiring up

- **DID `1E32`** (BMS): cumulative kWh charged / discharged over pack
  lifetime. Multi-frame, decoded as `U32 / 8583.07212` per EVNotify.
  Would let us cross-check the `2AB2` capacity estimate against
  observed throughput.
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
