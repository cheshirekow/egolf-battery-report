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
- **Gateway DID `2AB6`** — dumped raw, layout undetermined. Was
  briefly decoded as a pack-current cross-check, but three captures
  showed that byte field is a constant, not current (see *Experimental
  findings*). Reliable pack current comes from BMS DID `1E3D`.
- **Cell-module temperatures** — DID `2AB7`. 7 sensors in
  deciCelsius (`raw / 10 = °C`), plus 7 placeholder slots that always
  read 5.0 °C on this car.

Derived from the above:

- **State of Health** = `2AB2 max energy / 35.8 kWh` (the e-Golf's
  nominal gross capacity, per EVNotify). A third capture confirmed
  `2AB2` is a **slowly-updated BMS capacity estimate, independent of
  SoC** — so SoH (~91 %) is a trustworthy slow-moving indicator, not a
  bit-stable constant. See *Experimental findings* below.
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

# Emit a JSON object of all decoded values instead of the text report
python3 get_battery.py --output-format=json
python3 get_battery.py --cells --output-format=json > report.json
```

The script assumes the adapter is at `/dev/ttyUSB0`, 115200 baud. Adjust
the constants at the top of `get_battery.py` if your setup differs.

The car must be powered on (ignition on or "ready") for the BMS to
answer.

### Output formats

`--output-format` accepts `txt` (default) or `json`:

- **`txt`** — the human-readable report shown throughout this README.
- **`json`** — a single JSON object (indent 2) with every decoded value,
  written to stdout. Status/diagnostic lines go to stderr instead, so
  `> report.json` captures clean JSON. Keys include `soc_gross_pct`,
  `pack_voltage_v`, `pack_current_a`, `cell_balance`, `cell_voltages`
  (only with `--cells`), `max_energy_kwh`, `state_of_health_pct`,
  `current_usable_energy_kwh`, `module_temps_c`, and the raw
  `gateway_2ab6_raw` bytes.

### Converting existing text reports to JSON

`txt_to_json.py` re-saves a previously-captured text report as the same
JSON object, without needing the car:

```bash
python3 txt_to_json.py results-01.txt              # JSON to stdout
python3 txt_to_json.py results-01.txt -o results-01.json
```

It shares `report_format.py` (schema, ISO-TP decoding, summary math) with
`get_battery.py`, so a live `--output-format=json` capture and a converted
text report produce identical JSON for the same data. The three example
captures are checked in alongside their converted `results-0N.json`.

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

Three full captures were taken at meaningfully different SoC to cross-
check the decode formulas and to verify which values are static
properties of the pack vs. dynamic readouts. The artifacts are
[`results-01.txt`](results-01.txt), [`results-02.txt`](results-02.txt),
and [`results-03.txt`](results-03.txt).

| signal | run 1 (`results-01.txt`) | run 2 (`results-02.txt`) | run 3 (`results-03.txt`) |
|---|---|---|---|
| Ambient air temperature | ~80 °F (~27 °C) | ~70 °F (~21 °C) | ~65 °F (~18 °C) |
| Gross SoC (DID `028C`) | 95.6 % | 47.2 % | 78.4 % |
| Pack voltage (DID `1E3B`) | 361.0 V | 324.2 V | 344.8 V |
| Pack current (DID `1E3D`) | +0.0 A (rest) | −1.2 A (12 V keep-alive) | +0.0 A (rest) |
| Max cell (DID `1E33`) | 4.108 V, cell #51 | 3.692 V, cell #60 | 3.926 V, cell #63 |
| Min cell (DID `1E34`) | 4.099 V, **cell #32** | 3.683 V, **cell #32** | 3.916 V, **cell #32** |
| Cell-spread | 9.0 mV | 9.0 mV | 10.0 mV |
| Max energy raw (`2AB2`) | `3E 02 04 00` | `40 02 04 00` | `40 02 04 00` |
| Max energy (`2AB2` u16 × 2) | 31.75 kWh | 32.77 kWh | 32.77 kWh |
| Derived SoH | 88.7 % | 91.5 % | 91.5 % |
| Cell-module temps (`2AB7` mean) | 14.1 °C | 13.6 °C | 13.6 °C |

### Solid: pack-level decodes

Pack voltage and SoC moved together along the expected Li-ion discharge
curve — a 48 percentage-point SoC drop produced a 36.8 V pack-voltage
drop, which works out to 0.42 V per cell over 88 cells. Cell-module
temperatures changed only ~0.5 °C despite ~6 °C ambient swing, which is
consistent with the pack's thermal mass smoothing short-term ambient
changes.

### Solid: cell-spread and weak-cell identification

`1E33` / `1E34` reported a 9–10 mV cell-to-cell spread in all three
runs. More usefully, the **minimum cell was #32 in all three runs** —
the same physical cell shows up as the laggard regardless of SoC,
which is the exact signal you want from a balance check. The spread is
small and healthy; #32 is the cell to watch over future captures.

### Resolved: per-cell sweep encoding

The earlier `(raw / 4096)` decode in `sweep_cell_voltages` produced
implausible per-cell values (~0.758 V at high SoC; ~0.657 V at low
SoC). Cross-checking against the aggregate `1E33` / `1E34` actual
voltages showed a near-exact match under a different formula:

```
V_volts = (u16 BE raw / 1000) + 1.0
```

i.e. the raw value is millivolts above a 1.0 V baseline. Cross-SoC
consistency check (verified across all three captures):

| SoC | sweep avg as `/4096` | implied raw (u16 BE) | + 1000 mV | aggregate `1E33`/`1E34` actual |
|---|---|---|---|---|
| 95.6 % | 0.7582 V | 3105 | 4.105 V | 4.099 – 4.108 V |
| 78.4 % | (post-fix) | — | 3.923 V | 3.916 – 3.926 V |
| 47.2 % | 0.6566 V | 2690 | 3.690 V | 3.683 – 3.692 V |

The sweep's reported "spread" in raw counts also matches `1E33` /
`1E34`. The fix is committed; the per-cell sweep now produces real
volts, and the run-3 sweep average (3.923 V) lands squarely between
the aggregate min/max, confirming the formula at a third SoC.

### Resolved: DID `2AB2` is a SoC-independent capacity estimate

The first two captures showed `2AB2` moving (31.75 → 32.77 kWh), which
left open whether the `× 2` scaling was wrong or the value was dynamic.
The third capture settled it:

- Run 1 (95.6 % SoC): raw `3E 02 04 00` → **31.75 kWh**
- Run 2 (47.2 % SoC): raw `40 02 04 00` → **32.77 kWh**
- Run 3 (78.4 % SoC): raw `40 02 04 00` → **32.77 kWh**

Runs 2 and 3 are **byte-identical** despite a 31-point SoC difference,
and the highest-SoC run (run 1) returned the *lowest* value — so `2AB2`
is **not** SoC-derived. The most consistent explanation is that it's a
slowly-updated BMS capacity estimate: it stepped up once (`3E02` →
`4002`) after the deep discharge between runs 1 and 2 gave the
estimator a long sweep to recalibrate against, then held steady. So the
derived **SoH (~91 %) is a trustworthy slow-moving indicator**, just
not a bit-stable nameplate.

One refinement for future scaling work: the `2AB2` low byte was `0x02`
in all three runs — only the high byte moved (`3E → 40 → 40`). That
hints the energy may live in the high byte alone with the low byte as a
flags field. Both "high byte only" and "u16 × 2" land at ~31–33 kWh, so
it doesn't change the SoH conclusion, but it's the thread to pull if a
more precise value is ever needed.

### Resolved: the gateway `2AB6` "current cross-check" was decoding a constant

Earlier the gateway DID `2AB6` was decoded as a pack-current cross-check
from bytes [4..5]. Three captures killed that interpretation:

| run | BMS `1E3D` | `2AB6` bytes [4..5] | decoded "current" |
|---|---|---|---|
| 1 | +0.0 A | `07 FD` | +0.25 A |
| 2 | −1.2 A | `07 FD` | +0.25 A |
| 3 | +0.0 A | `07 FD` | +0.25 A |

Bytes [4..5] are pinned at `0x07FD` regardless of actual current —
including run 2, where the BMS clearly read −1.2 A — so they are a
constant field, not current. The earlier apparent "agreement" at rest
was a coincidence, and the apparent "disagreement" in run 2 was an
artifact of decoding the wrong (constant) field. There was never a real
gateway current reading to reconcile.

The genuinely varying data in `2AB6` is bytes [0..3], a mirrored u16
pair reading 120 / 62 / 109 across the three runs. That doesn't fit
SoC, pack voltage, or current under any linear scaling tried, so its
meaning is still unknown. The script now just dumps `2AB6` raw; reliable
pack current already comes from BMS DID `1E3D` (whose sign convention
run 2 confirmed: negative = discharge).

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

### Pin down `2AB2` scaling precisely (optional)

The three captures established that `2AB2` is a SoC-independent capacity
estimate (~32 kWh, ~91 % SoH) — see *Experimental findings*. What's
left is only the exact scaling: the constant low byte (`0x02`) suggests
the value may be high-byte-only rather than `u16 × 2`, and an ODIS-E
session capture showing VW's own "Maximum Energy Content" alongside the
raw `2AB2` bytes would settle it. Not urgent — both interpretations
land within ~1 kWh.

### Identify the varying `2AB6` field

`2AB6` bytes [0..3] are a mirrored u16 pair that read 120 / 62 / 109
across the three captures but don't fit SoC, voltage, or current under
any linear scaling tried. The script dumps it raw. More captures (ideally
during charge/discharge at known current) might reveal what it tracks.
Low priority — we already have everything useful from other DIDs.

### Track cell #32 over time

Cell #32 has now been the weakest cell in **all three** captures. Worth
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
