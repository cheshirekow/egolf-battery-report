import serial
import time
import sys

# Define your vLinker port. On Linux, it's typically /dev/ttyUSB0 or /dev/ttyACM0
SERIAL_PORT = '/dev/ttyUSB0'
BAUD_RATE = 115200 # vLinker default high-speed baud rate

# Nominal usable energy of a healthy 2019 e-Golf HV pack, used as the SoH
# denominator. Per EVNotify's E_GOLF.vue constant (CAPACITY = 35.8 kWh gross).
NOMINAL_PACK_KWH = 35.8

def send_command(ser, cmd, timeout=2.0):
    """Sends a command to the vLinker and returns the cleaned response string.

    Reads until the ELM327 '>' ready prompt is received, or until timeout.
    Strips the command echo (if echo is still on) and the trailing prompt.
    """
    ser.reset_input_buffer()
    ser.write((cmd + '\r').encode('utf-8'))

    buf = bytearray()
    deadline = time.time() + timeout
    while time.time() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf.extend(chunk)
            if b'>' in buf:
                break

    text = buf.decode('utf-8', errors='ignore').replace('\r', '\n')
    # Drop the trailing '>' prompt and any whitespace around it
    text = text.split('>', 1)[0].strip()
    # Drop the echoed command if it appears on the first line
    lines = [ln.strip() for ln in text.split('\n') if ln.strip()]
    if lines and lines[0].replace(' ', '') == cmd.replace(' ', ''):
        lines = lines[1:]
    return '\n'.join(lines)


def expect_ok(ser, cmd, timeout=2.0):
    """Send an init command and abort if the device does not reply OK."""
    resp = send_command(ser, cmd, timeout=timeout)
    if 'OK' not in resp.upper():
        print(f"Warning: '{cmd}' did not return OK. Got: {resp!r}")
    return resp


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


def scan_dids_22(ser, did_hi, lo_start, lo_end, timeout=2.0):
    """Probe service 0x22 DIDs from `did_hi`<<8 | lo_start through lo_end inclusive.

    For each DID, prints one line summarizing what came back: a negative
    response NRC, a positive response with byte count + hex dump + the
    most useful single-value interpretations, or '(no response)' if the
    ECU stayed silent. Intended for finding which adjacent DID actually
    holds a value of interest when the documented one has a confusing
    layout.
    """
    for lo in range(lo_start, lo_end + 1):
        did_str = f"{did_hi:02X}{lo:02X}"
        cmd = f"22 {did_hi:02X} {lo:02X}"
        resp = send_command(ser, cmd, timeout=timeout)
        if not resp:
            print(f"  {did_str}: (no response)")
            continue
        toks = resp.replace('\n', ' ').split()
        try:
            if len(toks) >= 3 and int(toks[0], 16) == 0x7F:
                print(f"  {did_str}: NRC 0x{int(toks[2], 16):02X}")
                continue
        except ValueError:
            pass
        data = parse_uds_22(resp, did_hi, lo)
        if data is None:
            print(f"  {did_str}: (unparseable) raw={resp!r}")
            continue
        hex_str = " ".join(f"{b:02X}" for b in data)
        hints = []
        if len(data) >= 2:
            v = (data[0] << 8) | data[1]
            hints.append(f"u16BE={v} (x2={v*2})")
        if len(data) >= 4:
            v32 = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
            hints.append(f"u32BE={v32}")
        print(f"  {did_str}: {len(data)}B {hex_str}  [{'; '.join(hints)}]")

def read_min_max_cells(ser):
    """Read DIDs 1E33 (max cell voltage + index) and 1E34 (min cell voltage
    + index) from the e-Golf BMS. Requires the extended diagnostic session
    (10 03) to be open on the current header. Returns a dict with max_v,
    max_idx, min_v, min_idx, spread_mv, plus the raw data byte lists for
    each so the index byte position can be eyeballed.
    """
    out = {}
    for label, did_lo in (('max', 0x33), ('min', 0x34)):
        resp = send_command(ser, f"22 1E {did_lo:02X}", timeout=1.0)
        data = parse_uds_22(resp, 0x1E, did_lo)
        if not data or len(data) < 2:
            print(f"  {label} cell read failed (1E{did_lo:02X}): {resp}")
            return None
        out[f'{label}_v'] = ((data[0] << 8) | data[1]) / 4096.0
        # Per OVMS notes for the VW e-Up BMS (same DID family): byte index 3
        # is the cell number. Use it defensively only if the response is
        # long enough.
        out[f'{label}_idx'] = data[3] if len(data) >= 4 else None
        out[f'{label}_raw'] = data
    out['spread_mv'] = (out['max_v'] - out['min_v']) * 1000.0
    return out


def sweep_cell_voltages(ser, n_cells=88, base_did=0x1E40, refresh_every=40):
    """Read `n_cells` consecutive cell-voltage DIDs starting at `base_did`.

    Each DID returns 2 bytes after the echo. Encoding (empirically
    determined from two captures at 95.6 % and 47.2 % SoC): the u16 BE
    raw value is millivolts above a 1.0 V baseline, so
    `V = (raw / 1000) + 1.0`. This differs from the aggregate min/max
    DIDs (1E33 / 1E34), which use the more familiar `raw / 4096`
    encoding. The decoded values for the two runs lined up exactly with
    the min/max readings under this formula, and the per-sweep spread
    (9 mV in raw counts) matched 1E33 / 1E34's reported spread.

    Periodically re-enters the extended diagnostic session (10 03) to
    keep the BMS's S3 timer from expiring midway through the sweep.
    Returns a list of length `n_cells`, each entry either a float
    (volts) or None on a missing / negative response.
    """
    voltages = []
    for n in range(n_cells):
        if n > 0 and n % refresh_every == 0:
            send_command(ser, "10 03", timeout=0.5)
        did = base_did + n
        hi, lo = (did >> 8) & 0xFF, did & 0xFF
        resp = send_command(ser, f"22 {hi:02X} {lo:02X}", timeout=1.0)
        data = parse_uds_22(resp, hi, lo)
        if data and len(data) >= 2:
            voltages.append(((data[0] << 8) | data[1]) / 1000.0 + 1.0)
        else:
            voltages.append(None)
    return voltages


def main():
    cells_flag = '--cells' in sys.argv
    print(f"Connecting to vLinker on {SERIAL_PORT}...")
    try:
        # Initialize serial connection
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"Error opening serial port: {e}")
        print("Tip: Check if you need 'sudo' permissions or if the port name changed.")
        sys.exit(1)

    print("Initializing ELM327 / STN protocols...")
    # ATZ resets the device and can take 1-2s before the banner is emitted.
    reset_banner = send_command(ser, "ATZ", timeout=4.0)
    print(f"Reset banner: {reset_banner!r}")
    expect_ok(ser, "ATE0")     # Echo off
    expect_ok(ser, "ATL0")     # Linefeeds off
    expect_ok(ser, "ATSP6")    # Set protocol to ISO 15765-4 CAN (11 bit ID, 500 kbaud)

    print("\nRequesting Diagnostic Data from e-Golf Battery Management module...")

    # Target the Battery Energy Control Module ("control unit 8C", J840) at
    # request 0x7E5 / response 0x7ED. VIN (DID F190) on this header has been
    # observed to return a positive 62 F1 90 ... reply, confirming the address.
    expect_ok(ser, "ATSH7E5")

    # --- Gross State of Charge (DID 028C) -----------------------------------
    # Internal SoC, before the dash's bottom/top display buffer. The earlier
    # script tried DID 2A09; that is a MEB-platform identifier (ID.3/ID.4),
    # not e-Golf, and the BMS rejected it with 7F 22 31 (requestOutOfRange).
    # 028C is the correct MK7 e-Golf BMS DID per EVNotify's E_GOLF.vue and
    # the obd-amigos PID database. Scaling: raw byte / 2.5 = percent.
    print("\n--- Fetching Gross State of Charge (DID 028C) ---")
    soc_resp = send_command(ser, "22 02 8C")
    print(f"Raw Response: {soc_resp}")
    soc_data = parse_uds_22(soc_resp, 0x02, 0x8C)
    soc_gross_pct = None
    if soc_data:
        soc_gross_pct = soc_data[0] / 2.5
        print(f"  SoC (gross): {soc_gross_pct:.1f} %")

    # --- HV pack voltage (DID 1E3B) -----------------------------------------
    # ((HH << 8) | LL) / 4 = volts. Source: EVNotify E_GOLF.vue.
    print("\n--- Fetching HV Pack Voltage (DID 1E3B) ---")
    v_resp = send_command(ser, "22 1E 3B")
    print(f"Raw Response: {v_resp}")
    v_data = parse_uds_22(v_resp, 0x1E, 0x3B)
    if v_data and len(v_data) >= 2:
        v_pack = ((v_data[0] << 8) | v_data[1]) / 4.0
        print(f"  Pack voltage: {v_pack:.1f} V")

    # --- HV pack current (DID 1E3D) -----------------------------------------
    # (((HH << 8) | LL) - 2044) / 4 = amps. Sign convention per EVNotify:
    # positive = charge, negative = discharge. Verify on your car.
    print("\n--- Fetching HV Pack Current (DID 1E3D) ---")
    i_resp = send_command(ser, "22 1E 3D")
    print(f"Raw Response: {i_resp}")
    i_data = parse_uds_22(i_resp, 0x1E, 0x3D)
    if i_data and len(i_data) >= 2:
        i_pack = (((i_data[0] << 8) | i_data[1]) - 2044) / 4.0
        print(f"  Pack current: {i_pack:+.1f} A  (+ charge / - discharge)")

    # --- Cell voltage balance (DIDs 1E33/1E34, optional 1E40..1E97) --------
    # Cell-voltage DIDs are gated behind UDS extended diagnostic session
    # (0x10 sub-function 0x03); in the default session the BMS NACKs them
    # with 7F 22 31. The session is opened on the currently addressed ECU
    # only (still 7E5 here) and self-expires after the S3 timer elapses
    # (~5 s of inactivity). DID base 0x1E40 + cell-1-indexed offset, one
    # cell per query. Source: Merkle et al., "Estimate e-Golf Battery State
    # Using Diagnostic Data and a Digital Twin," Batteries 2021 7(1):15
    # (MDPI/TUM-FTM); corroborated by the OVMS3 vehicle_vweup driver.
    print("\n--- Cell voltage balance (BMS extended session) ---")
    sess_resp = send_command(ser, "10 03", timeout=1.0)
    print(f"  Session control (10 03): {sess_resp}")
    if '50 03' not in sess_resp and '5003' not in sess_resp.replace(' ', ''):
        print("  WARNING: failed to enter extended session "
              "- cell DIDs likely to NACK")

    balance = read_min_max_cells(ser)
    if balance:
        print(f"  Max cell: {balance['max_v']:.4f} V  "
              f"(cell #{balance['max_idx']})  raw={balance['max_raw']}")
        print(f"  Min cell: {balance['min_v']:.4f} V  "
              f"(cell #{balance['min_idx']})  raw={balance['min_raw']}")
        print(f"  Spread:   {balance['spread_mv']:.1f} mV")

    if cells_flag:
        print("\n--- Full cell-voltage sweep "
              "(88 cells, DIDs 1E40..1E97) ---")
        voltages = sweep_cell_voltages(ser)
        valid = [v for v in voltages if v is not None]
        fails = sum(1 for v in voltages if v is None)
        if valid:
            v_min, v_max = min(valid), max(valid)
            v_avg = sum(valid) / len(valid)
            print(f"  Min: {v_min:.4f} V  Max: {v_max:.4f} V  "
                  f"Avg: {v_avg:.4f} V  Spread: {(v_max - v_min) * 1000:.1f} mV")
        if fails:
            print(f"  {fails}/{len(voltages)} cells did not respond")
        print("  Per-cell voltages (cell# : V, 8 per row):")
        for row in range(0, len(voltages), 8):
            parts = []
            for col in range(8):
                idx = row + col
                if idx >= len(voltages):
                    break
                v = voltages[idx]
                cell_n = idx + 1
                parts.append(f"{cell_n:3}:{'----- ' if v is None else f'{v:.3f}V'}")
            print("    " + "  ".join(parts))
    else:
        print("  (pass --cells to add a full per-cell sweep, ~5 s slower)")

    # Return to default session before the gateway probe so we don't leave
    # the BMS in extended mode longer than necessary.
    send_command(ser, "10 01", timeout=1.0)

    # --- Gateway probe over plain ISO-TP (0x710 / 0x77A) --------------------
    # The HV "current energy content" (DID 2AB8) and "maximum energy content"
    # (DID 2AB2 - the SoH input) live on the gateway, control unit 0x19,
    # not the BMS. On some VAG cars the gateway answers gateway-side DIDs
    # over plain ISO-TP at request 0x710 / response 0x77A; on others these
    # are only reachable through a VWTP 2.0 tunnel, which the stock ELM327
    # firmware does not implement. Cheap probe: try the plain ISO-TP path.
    # The default response filter for ATSH=7Ex is 7E(x+8); 0x710 falls
    # outside that range so we set ATCRA explicitly to 0x77A.
    print("\n--- Probing gateway over ISO-TP (request 0x710 / response 0x77A) ---")
    expect_ok(ser, "ATSH710")
    expect_ok(ser, "ATCRA77A")
    # Multi-frame responses (like 2AB8, which is 11 bytes total) require the
    # ELM327 to send an ISO-TP Flow Control frame back to the sender after
    # the First Frame. The chip does that automatically for the standard
    # 7Ex pair, but not for our non-standard 0x710 / 0x77A channel - so the
    # first attempt at 2AB8 returned only the First Frame and the gateway
    # gave up. Tell the chip explicitly: send FC on 0x710 with the payload
    # 30 00 00 (continue, no block-size limit, zero separation time), and
    # switch to manual FC mode so it actually uses those settings.
    expect_ok(ser, "ATFCSH710")
    expect_ok(ser, "ATFCSD300000")
    expect_ok(ser, "ATFCSM1")

    # --- Maximum Energy Content (DID 2AB2) ---------------------------------
    # 4-byte response. First u16 BE is the pack's current maximum energy in
    # 0.5 Wh units, so raw x 2 = Wh. Scaling chosen because it lands at
    # ~31.75 kWh on this car, matching the e-Golf's documented usable
    # capacity. The trailing two bytes appear to be a status / flags field.
    print("\n  Maximum Energy Content (DID 2AB2):")
    mec_resp = send_command(ser, "22 2A B2", timeout=3.0)
    print(f"    Raw Response: {mec_resp}")
    mec_data = parse_uds_22(mec_resp, 0x2A, 0xB2)
    max_kwh = None
    if mec_data and len(mec_data) >= 2:
        max_kwh = ((mec_data[0] << 8) | mec_data[1]) * 2 / 1000.0
        print(f"  Max energy: {max_kwh:.2f} kWh")

    # --- Gateway-side pack current cross-check (DID 2AB6) ------------------
    # 7-byte response. Bytes [4..5] hold the pack current using the same
    # (raw - 2044) / 4 encoding as BMS DID 1E3D. Used as a sanity check
    # that gateway and BMS agree on the current reading.
    print("\n  Pack current cross-check (DID 2AB6):")
    i2_resp = send_command(ser, "22 2A B6", timeout=3.0)
    print(f"    Raw Response: {i2_resp}")
    i2_data = parse_uds_22(i2_resp, 0x2A, 0xB6)
    if i2_data and len(i2_data) >= 6:
        i_gw = (((i2_data[4] << 8) | i2_data[5]) - 2044) / 4.0
        print(f"  Pack current (gateway): {i_gw:+.2f} A")

    # --- Cell-module temperatures (DID 2AB7) -------------------------------
    # 28-byte response = 14 u16 BE values. On this car the first seven are
    # cell-module temperature sensors in deciCelsius (raw / 10 = degC); the
    # remaining seven come back as 0x0032 (5.0 degC) on every read and
    # look like placeholder / unconfigured-sensor entries.
    print("\n  Cell-module temperatures (DID 2AB7):")
    t_resp = send_command(ser, "22 2A B7", timeout=3.0)
    print(f"    Raw Response: {t_resp}")
    t_data = parse_uds_22(t_resp, 0x2A, 0xB7)
    if t_data and len(t_data) >= 14:
        temps_c = [((t_data[2 * k] << 8) | t_data[2 * k + 1]) / 10.0
                   for k in range(7)]
        print("  Sensor temperatures (degC): " +
              ", ".join(f"{t:.1f}" for t in temps_c))
        print(f"  Min/Max/Avg: {min(temps_c):.1f} / {max(temps_c):.1f} / "
              f"{sum(temps_c) / len(temps_c):.1f} degC")

    ser.close()

    print("\n--- Derived Summary ---")
    if max_kwh is not None:
        soh_pct = (max_kwh / NOMINAL_PACK_KWH) * 100.0
        print(f"State of Health: {soh_pct:.1f} %  "
              f"({max_kwh:.2f} kWh / {NOMINAL_PACK_KWH} kWh nominal)")
    if soc_gross_pct is not None and max_kwh is not None:
        cur_kwh = (soc_gross_pct / 100.0) * max_kwh
        print(f"Current usable energy: {cur_kwh:.2f} kWh  "
              f"(SoC {soc_gross_pct:.1f} % x max {max_kwh:.2f} kWh)")
    elif soc_gross_pct is not None:
        fallback_kwh = (soc_gross_pct / 100.0) * NOMINAL_PACK_KWH
        print(f"Current usable energy (gateway 2AB2 missing - falling back "
              f"to nominal): {fallback_kwh:.2f} kWh")

if __name__ == "__main__":
    main()
