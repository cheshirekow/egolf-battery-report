import serial
import time
import sys

# Define your vLinker port. On Linux, it's typically /dev/ttyUSB0 or /dev/ttyACM0
SERIAL_PORT = '/dev/ttyUSB0'
BAUD_RATE = 115200 # vLinker default high-speed baud rate

def send_command(ser, cmd, delay=0.2):
    """Sends a command to the vLinker and returns the cleaned response string."""
    full_cmd = (cmd + '\r').encode('utf-8')
    ser.write(full_cmd)
    time.sleep(delay)

    response = ser.read_all().decode('utf-8', errors='ignore')
    # Clean up formatting strings, carriage returns, and echoes
    cleaned = response.replace('\r', '\n').strip()
    return cleaned

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
    send_command(ser, "ATZ")      # Reset device
    send_command(ser, "ATE0")     # Echo off
    send_command(ser, "ATL0")     # Linefeeds off
    send_command(ser, "ATSP6")    # Set protocol to ISO 15765-4 CAN (11 bit ID, 500 kbaud)

    print("\nRequesting Diagnostic Data from e-Golf Gateway...")

    # Set the CAN targeting ID to the Gateway module (UDS Address 0x19 / 0x7E0 variant)
    send_command(ser, "ATSH7E0")

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
