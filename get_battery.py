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
    ('014\\n0: 62 F1 90 2D 2D 2D\\n1: 2D ...'). Returns the list of data
    bytes that follow the '62 <hi> <lo>' echo, or None if the response
    is missing, negative (7F 22 NRC), or does not match the requested DID.
    """
    if not resp:
        return None
    hex_bytes = []
    for line in resp.split('\n'):
        line = line.strip()
        if not line:
            continue
        # Drop a leading ISO-TP length header like "014" (3 hex digits, no spaces)
        if ' ' not in line and len(line) <= 4:
            continue
        # Strip a frame index prefix like "0:" / "1:"
        if ':' in line:
            line = line.split(':', 1)[1]
        for tok in line.split():
            try:
                hex_bytes.append(int(tok, 16))
            except ValueError:
                pass
    if len(hex_bytes) < 3 or hex_bytes[0] == 0x7F:
        return None
    if hex_bytes[0] != 0x62 or hex_bytes[1] != did_hi or hex_bytes[2] != did_lo:
        return None
    return hex_bytes[3:]

def main():
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

    def _print_scaling_candidates(data):
        """Print 2-byte BE under common VW energy-DID scalings."""
        if len(data) < 2:
            return
        raw = (data[0] << 8) | data[1]
        print(f"    raw u16 BE = {raw}")
        print(f"      x1 Wh : {raw/1000:8.3f} kWh")
        print(f"      x2 Wh : {raw*2/1000:8.3f} kWh   (raw in 0.5 Wh units)")
        print(f"      x10 Wh: {raw*10/1000:8.3f} kWh  (raw in 10 Wh units)")
        if len(data) >= 4:
            be4 = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
            print(f"    raw u32 BE = {be4}")
            print(f"      x1 Wh : {be4/1000:8.3f} kWh")

    print("\n  Maximum Energy Content (DID 2AB2):")
    mec_resp = send_command(ser, "22 2A B2", timeout=3.0)
    print(f"    Raw Response: {mec_resp!r}")
    mec_data = parse_uds_22(mec_resp, 0x2A, 0xB2)
    max_kwh = None
    if mec_data and len(mec_data) >= 2:
        _print_scaling_candidates(mec_data)
        # Guess: raw x 2 Wh = kWh lands at ~31.75 kWh for this car, which is
        # close to the e-Golf's documented usable capacity (~31.5 kWh) and
        # implies SoH near 100%. Use that as the working hypothesis; flag
        # the SoH line below as "unverified scaling".
        be2 = (mec_data[0] << 8) | mec_data[1]
        max_kwh = (be2 * 2) / 1000.0
    elif mec_resp:
        print("    (no positive response - gateway likely needs VWTP 2.0)")

    print("\n  Current Energy Content (DID 2AB8):")
    cec_resp = send_command(ser, "22 2A B8", timeout=3.0)
    print(f"    Raw Response: {cec_resp!r}")
    cec_data = parse_uds_22(cec_resp, 0x2A, 0xB8)
    if cec_data and len(cec_data) >= 2:
        print(f"    Data bytes ({len(cec_data)}): " +
              " ".join(f"{b:02X}" for b in cec_data))
        _print_scaling_candidates(cec_data)
    elif cec_resp:
        print("    (no positive response - gateway likely needs VWTP 2.0)")

    ser.close()

    print("\n--- Derived Energy & SoH Notes ---")
    if soc_gross_pct is not None:
        usable_kwh = (soc_gross_pct / 100.0) * NOMINAL_PACK_KWH
        print(f"Approx usable energy now (SoC x nominal): {usable_kwh:.2f} kWh "
              f"(SoC {soc_gross_pct:.1f}% x {NOMINAL_PACK_KWH} kWh).")
    if max_kwh is not None:
        soh_pct = (max_kwh / NOMINAL_PACK_KWH) * 100.0
        print(f"State of Health (gateway 2AB2 / nominal): {soh_pct:.1f} %  "
              f"({max_kwh:.2f} kWh / {NOMINAL_PACK_KWH} kWh).")
        print("  NOTE: 2AB2 scaling is unverified - assumed raw x 2 = Wh "
              "(0.5 Wh units). Cross-check against the candidates printed "
              "in the gateway probe section.")
    else:
        print("True State of Health was not readable on this run. The gateway")
        print("probe above either returned no data or a negative response; on")
        print("this car DID 2AB2 likely requires a VWTP 2.0 tunnel, which the")
        print("stock ELM327 firmware does not implement. The closest BMS-side")
        print("proxy is to log SoC + current over a full charge cycle and")
        print("integrate to estimate present capacity.")

if __name__ == "__main__":
    main()
