import serial
import time
import sys

# Define your vLinker port. On Linux, it's typically /dev/ttyUSB0 or /dev/ttyACM0
SERIAL_PORT = '/dev/ttyUSB0'
BAUD_RATE = 115200 # vLinker default high-speed baud rate

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

    # Target the Battery Energy Control Module directly. The gateway at 0x7E0
    # responds to UDS but does not host the HV battery DIDs (it returned
    # 7F 22 31 / requestOutOfRange for 2A0A and 2A09). On VAG MQB platforms the
    # high voltage battery management module typically answers at 0x7E5 (request)
    # / 0x7ED (response).
    expect_ok(ser, "ATSH7E5")

    # Sanity-check the path with a DID every UDS-capable module supports.
    # A positive response is "62 F1 90" followed by 17 ASCII VIN bytes.
    print("\n--- Sanity check: reading VIN (DID F190) ---")
    vin_resp = send_command(ser, "22 F1 90")
    print(f"Raw Response: {vin_resp}")

    # 1. Query Current Energy Content (kWh)
    # 222A0A is the common VAG UDS PID for High Voltage Battery Energy Information
    print("\n--- Fetching Battery Energy Content ---")
    energy_resp = send_command(ser, "22 2A 0A")
    print(f"Raw Response: {energy_resp}")

    # 2. Query Current Battery Charge State (%)
    print("\n--- Fetching High Voltage Battery Charge State ---")
    soc_resp = send_command(ser, "22 2A 09")
    print(f"Raw Response: {soc_resp}")

    ser.close()

    print("\n--- How to Calculate Your State of Health (SOH) ---")
    print("Because UDS hex data needs strict byte extraction, look at your output:")
    print("1. Take the remaining Energy Capacity value (decoded from your response hex).")
    print("2. Take the Displayed SOC % fraction (e.g., 0.90 for 90%).")
    print("3. Formula: Remaining Capacity / SOC% = Total Available kWh capacity.")
    print("4. Divide your total available capacity by 32.0 kWh (the 2019 e-Golf factory usable total) to get your SOH %.")

if __name__ == "__main__":
    main()
