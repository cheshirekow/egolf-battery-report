"""Shared schema, UDS decoding, and summary helpers for the e-Golf report.

Imported by both `get_battery.py` (live capture) and `txt_to_json.py`
(converting an existing textual report) so the two paths emit byte-identical
JSON for the same underlying data. Deliberately free of any hardware
dependency (no pyserial import) so the converter runs anywhere.

The JSON schema is defined in one place, `new_report()`. Numeric values are
rounded to the same precision the text report displays, so a value decoded
live and the same value parsed back out of a text capture land on the same
number.
"""


def parse_uds_22(resp, did_hi, did_lo):
    """Extract the data payload from a UDS service 0x22 positive response.

    Accepts both the single-frame ELM327 form ('62 02 8C AA') and the
    multi-frame form with ISO-TP length header and frame indices
    ('014\\n0: 62 F1 90 2D 2D 2D\\n1: 2D ...'). When a length header is
    present we truncate to its value, which discards any CAN-frame
    padding (e.g. trailing 'AA AA') the chip leaves in the dump for the
    final consecutive frame. Returns the list of data bytes that follow
    the '62 <hi> <lo>' echo, or None if the response is missing,
    negative (7F 22 NRC), or does not match the requested DID.
    """
    if not resp:
        return None
    declared_len = None
    hex_bytes = []
    for line in resp.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Capture the ISO-TP length header (3 hex digits, no spaces).
        # It tells us how many bytes of UDS payload actually belong to
        # this message - the remainder is CAN padding.
        if ' ' not in line and 1 <= len(line) <= 4:
            try:
                declared_len = int(line, 16)
                continue
            except ValueError:
                pass
        # Strip a frame index prefix like "0:" / "1:"
        if ':' in line:
            line = line.split(':', 1)[1]
        for tok in line.split():
            try:
                hex_bytes.append(int(tok, 16))
            except ValueError:
                pass
    if declared_len is not None:
        hex_bytes = hex_bytes[:declared_len]
    if len(hex_bytes) < 3 or hex_bytes[0] == 0x7F:
        return None
    if hex_bytes[0] != 0x62 or hex_bytes[1] != did_hi or hex_bytes[2] != did_lo:
        return None
    return hex_bytes[3:]


def new_report(nominal_pack_kwh):
    """Return an empty report dict with every key present (values None).

    Keeping the key set in one place guarantees the live capture and the
    txt converter produce the same JSON shape.
    """
    return {
        "soc_gross_pct": None,
        "pack_voltage_v": None,
        "pack_current_a": None,
        "cell_balance": None,
        "cell_voltages": None,
        "cell_voltage_summary": None,
        "max_energy_kwh": None,
        "gateway_2ab6_raw": None,
        "module_temps_c": None,
        "module_temp_summary": None,
        "state_of_health_pct": None,
        "current_usable_energy_kwh": None,
        "nominal_pack_kwh": nominal_pack_kwh,
    }


def cell_voltage_summary(voltages):
    """min / max / avg / spread over per-cell volts (None entries skipped).

    `voltages` should already be rounded to display precision so that the
    live path and the converter compute identical summaries.
    """
    valid = [v for v in voltages if v is not None]
    if not valid:
        return None
    vmin, vmax = min(valid), max(valid)
    return {
        "min_v": round(vmin, 3),
        "max_v": round(vmax, 3),
        "avg_v": round(sum(valid) / len(valid), 4),
        "spread_mv": round((vmax - vmin) * 1000.0, 1),
        "responded": len(valid),
        "total": len(voltages),
    }


def temp_summary(temps):
    """min / max / avg over a list of module temperatures in degC."""
    if not temps:
        return None
    return {
        "min_c": round(min(temps), 1),
        "max_c": round(max(temps), 1),
        "avg_c": round(sum(temps) / len(temps), 1),
    }
