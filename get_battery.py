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

    ser.close()

    print("\n--- Derived Energy & SoH Notes ---")
    if soc_gross_pct is not None:
        usable_kwh = (soc_gross_pct / 100.0) * NOMINAL_PACK_KWH
        print(f"Approx usable energy now: {usable_kwh:.2f} kWh "
              f"(SoC {soc_gross_pct:.1f}% x nominal {NOMINAL_PACK_KWH} kWh).")
    print("True State of Health (max-energy-content / nominal) is not exposed")
    print("on the BMS at 0x7E5. VW puts it on the gateway (unit 0x19) as DID")
    print("2AB2 (Maximum Energy Content, Wh). Reaching it requires a different")
    print("UDS channel (and on some cars a VWTP 2.0 tunnel) and is out of scope")
    print("for this script. The closest BMS-side proxy is to log SoC + current")
    print("over a full charge cycle and integrate to estimate present capacity.")

if __name__ == "__main__":
    main()
