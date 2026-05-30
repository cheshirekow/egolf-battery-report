#!/usr/bin/env python3
"""Convert a textual e-Golf battery report into the JSON form.

Reads a text report (the default output of get_battery.py) and emits the
same JSON object that `get_battery.py --output-format=json` produces, so
previously-captured .txt reports can be re-saved as JSON without re-running
against the car.

Usage:
    python3 txt_to_json.py results-01.txt              # JSON to stdout
    python3 txt_to_json.py results-01.txt -o out.json  # JSON to a file

Shares report_format.py with get_battery.py so the schema, the ISO-TP
decoding (used to recover the raw 2AB6 bytes from older reports), and the
summary computations are identical between the live and converted paths.
"""
import argparse
import json
import re
import sys

from report_format import (new_report, parse_uds_22, cell_voltage_summary,
                            temp_summary)

# Matches get_battery.NOMINAL_PACK_KWH; only used if the report's SoH line
# doesn't state the nominal capacity (it normally does).
DEFAULT_NOMINAL_PACK_KWH = 35.8


def _search_float(pattern, text):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _extract_2ab6_raw(text):
    """Return the 2AB6 data-byte hex string, handling both report formats.

    Newer reports print a literal 'Data bytes: ..' line. Older reports only
    printed the multi-frame 'Raw Response' block, so reconstruct that block
    and run it through the same ISO-TP decoder the live script uses.
    """
    # Newer format: 2AB6 is the only DID that emits a "Data bytes:" line.
    m = re.search(r'Data bytes:\s*([0-9A-Fa-f ]+)', text)
    if m:
        return m.group(1).strip().upper()

    # Older format: find the 2AB6 section, grab its 'Raw Response:' line plus
    # any following 'N: hex..' continuation frames, and decode.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if '2AB6' not in line:
            continue
        for j in range(i, min(i + 6, len(lines))):
            rr = re.match(r'\s*Raw Response:\s*(.*)', lines[j])
            if not rr:
                continue
            pieces = [rr.group(1).strip()]
            k = j + 1
            while k < len(lines) and re.match(r'\s*[0-9A-Fa-f]+:\s', lines[k]):
                pieces.append(lines[k].strip())
                k += 1
            data = parse_uds_22('\n'.join(p for p in pieces if p), 0x2A, 0xB6)
            return ' '.join(f'{b:02X}' for b in data) if data else None
        break
    return None


def _extract_cell_voltages(text):
    """Parse the per-cell sweep table into a 1-indexed list of volts.

    Cells that did not respond (printed as dashes) become None. Returns None
    if the report has no sweep section.
    """
    cells = {}
    for m in re.finditer(r'(\d+):(?:(\d+\.\d+)V|-+)', text):
        n = int(m.group(1))
        cells[n] = float(m.group(2)) if m.group(2) else None
    if not cells:
        return None
    return [cells.get(n) for n in range(1, max(cells) + 1)]


def convert(text):
    """Parse a text report into the shared JSON report dict."""
    report = new_report(DEFAULT_NOMINAL_PACK_KWH)

    report["soc_gross_pct"] = _search_float(r'SoC \(gross\):\s*([\d.]+)\s*%', text)
    report["pack_voltage_v"] = _search_float(r'Pack voltage:\s*([\d.]+)\s*V', text)
    report["pack_current_a"] = _search_float(
        r'Pack current:\s*([+-]?[\d.]+)\s*A', text)

    max_m = re.search(r'Max cell:\s*([\d.]+)\s*V\s*\(cell #(\d+)\)', text)
    min_m = re.search(r'Min cell:\s*([\d.]+)\s*V\s*\(cell #(\d+)\)', text)
    spread_m = re.search(r'Spread:\s*([\d.]+)\s*mV', text)
    if max_m and min_m:
        report["cell_balance"] = {
            "max_v": float(max_m.group(1)),
            "max_cell": int(max_m.group(2)),
            "min_v": float(min_m.group(1)),
            "min_cell": int(min_m.group(2)),
            "spread_mv": float(spread_m.group(1)) if spread_m else None,
        }

    voltages = _extract_cell_voltages(text)
    if voltages is not None:
        report["cell_voltages"] = voltages
        report["cell_voltage_summary"] = cell_voltage_summary(voltages)

    report["max_energy_kwh"] = _search_float(r'Max energy:\s*([\d.]+)\s*kWh', text)
    report["gateway_2ab6_raw"] = _extract_2ab6_raw(text)

    temps_m = re.search(r'Sensor temperatures \(degC\):\s*(.+)', text)
    if temps_m:
        temps = [float(x) for x in temps_m.group(1).split(',') if x.strip()]
        report["module_temps_c"] = temps
        report["module_temp_summary"] = temp_summary(temps)

    soh_m = re.search(
        r'State of Health:\s*([\d.]+)\s*%.*?/\s*([\d.]+)\s*kWh nominal', text)
    if soh_m:
        report["state_of_health_pct"] = float(soh_m.group(1))
        report["nominal_pack_kwh"] = float(soh_m.group(2))

    report["current_usable_energy_kwh"] = _search_float(
        r'Current usable energy[^:]*:\s*([\d.]+)\s*kWh', text)

    return report


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help="path to a textual report (e.g. results-01.txt)")
    ap.add_argument('-o', '--output',
                    help="write JSON to this file instead of stdout")
    args = ap.parse_args()

    with open(args.input, 'r', encoding='utf-8') as f:
        text = f.read()
    out = json.dumps(convert(text), indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(out + '\n')
    else:
        print(out)


if __name__ == '__main__':
    main()
