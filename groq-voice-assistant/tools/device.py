"""
ESP32 voice assistant - device tool.

  python tools/device.py flash      build and upload the firmware
  python tools/device.py setup      send this laptop's Wi-Fi to the ESP32
  python tools/device.py monitor    show the ESP32's live log
  python tools/device.py calibrate  learn your voice level
  python tools/device.py cmd tone   run any ESP32 command (help, status,
                                    mic, tone, hello, volume 60, ...)

Options: --port COM7   --arduino-data D:\\ArduinoData   (see --help)
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("Missing pyserial. Run:  python -m pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent.parent
SKETCH = ROOT / "firmware" / "voice_assistant"
SETTINGS_FILE = Path(__file__).resolve().parent / "device_settings.json"
FQBN = "esp32:esp32:XIAO_ESP32S3:PSRAM=opi,USBMode=hwcdc,CDCOnBoot=default"
ESP32_INDEX = ("https://raw.githubusercontent.com/espressif/"
               "arduino-esp32/gh-pages/package_esp32_index.json")
ESPRESSIF_VID = 0x303A
IS_WINDOWS = platform.system() == "Windows"


# ==========================================
# SAVED TOOL SETTINGS
# ==========================================
def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))


# ==========================================
# SERIAL PORT
# ==========================================
def find_port(requested):
    if requested:
        return requested
    ports = list(serial.tools.list_ports.comports())
    esp = [p for p in ports if p.vid == ESPRESSIF_VID]
    if len(esp) == 1:
        return esp[0].device
    if len(esp) > 1:
        sys.exit("Several ESP32 boards found: "
                 + ", ".join(p.device for p in esp) + ". Use --port.")
    if not ports:
        sys.exit("No serial port found. Plug the ESP32 in with a USB-C "
                 "*data* cable (some cables only charge).")
    listing = "\n".join(f"  {p.device}: {p.description}" for p in ports)
    sys.exit("No ESP32-S3 found automatically. Ports:\n"
             f"{listing}\nPick one with --port.")


def open_serial(port):
    """Open without toggling DTR/RTS, so the board doesn't reset."""
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = port, 115200, 0.2
    s.dtr = False
    s.rts = False
    for _ in range(20):
        try:
            s.open()
            return s
        except serial.SerialException as e:
            last = e
            time.sleep(0.5)
    sys.exit(f"Cannot open {port}: {last}\n"
             "Close the Arduino Serial Monitor or any other program using it.")


def read_lines(s, seconds, stop=None, show=True):
    """Print lines from the board for `seconds`, or until `stop(line)`
    returns something truthy (which is returned)."""
    end = time.time() + seconds
    buf = b""
    while time.time() < end:
        try:
            chunk = s.read(256)
        except serial.SerialException:
            # USB port drops for a moment while the board restarts.
            time.sleep(0.5)
            try:
                s.close()
                s.open()
            except serial.SerialException:
                pass
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").rstrip()
            if show and line:
                print("  ESP32 |", line)
            if stop:
                result = stop(line)
                if result:
                    return result
    return None


def send(s, line):
    s.write((line + "\n").encode("utf-8"))
    s.flush()
    time.sleep(0.15)


# ==========================================
# ARDUINO CLI
# ==========================================
def find_arduino_cli():
    found = shutil.which("arduino-cli")
    if found:
        return found
    candidates = []
    if IS_WINDOWS:
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
            for sub in (r"Programs\Arduino IDE", "Arduino IDE"):
                candidates.append(Path(base) / sub / "resources/app/lib/backend/resources/arduino-cli.exe")
    elif platform.system() == "Darwin":
        candidates.append(Path("/Applications/Arduino IDE.app/Contents/Resources/app/lib/backend/resources/arduino-cli"))
    else:
        candidates += [Path(p) / "resources/app/lib/backend/resources/arduino-cli"
                       for p in ("/opt/arduino-ide", "/usr/share/arduino-ide",
                                 str(Path.home() / "arduino-ide"))]
    for c in candidates:
        if c.exists():
            return str(c)
    sys.exit("arduino-cli not found. Install the Arduino IDE 2 "
             "(https://www.arduino.cc/en/software) or arduino-cli, then retry.")


def cli_env(data_dir):
    env = dict(os.environ)
    env["ARDUINO_BOARD_MANAGER_ADDITIONAL_URLS"] = ESP32_INDEX
    if data_dir:
        env["ARDUINO_DIRECTORIES_DATA"] = str(data_dir)
        env["ARDUINO_DIRECTORIES_DOWNLOADS"] = str(Path(data_dir) / "staging")
        env["ARDUINO_BUILD_CACHE_PATH"] = str(Path(data_dir) / "cache")
    return env


def run(cmd, env, check=True):
    print("  >", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    result = subprocess.run(cmd, env=env)
    if check and result.returncode != 0:
        sys.exit("Command failed (see messages above).")
    return result.returncode


def cmd_flash(args):
    settings = load_settings()
    if args.arduino_data:
        settings["arduino_data"] = args.arduino_data
        save_settings(settings)
    data_dir = settings.get("arduino_data") or os.environ.get("ARDUINO_DIRECTORIES_DATA")

    cli = find_arduino_cli()
    env = cli_env(data_dir)
    port = find_port(args.port)
    print(f"arduino-cli: {cli}")
    print(f"board data:  {data_dir or 'default location'}")
    print(f"port:        {port}")

    installed = subprocess.run([cli, "core", "list"], env=env,
                               capture_output=True, text=True).stdout
    if "esp32:esp32" not in installed:
        target = Path(data_dir) if data_dir else Path.home()
        free_gb = shutil.disk_usage(target.anchor or "/").free / 1e9
        if free_gb < 4:
            sys.exit(f"Only {free_gb:.1f} GB free on {target.anchor}. The ESP32 "
                     "tools need about 4 GB. Re-run with a roomier drive, e.g.\n"
                     "  python tools/device.py flash --arduino-data D:\\ArduinoData")
        print("\nInstalling ESP32 board support (one time, ~1 GB download)...")
        run([cli, "core", "update-index"], env)
        run([cli, "core", "install", "esp32:esp32"], env)

    build = (Path(data_dir) if data_dir else ROOT / "firmware") / "build" / "voice_assistant"
    print("\nCompiling firmware...")
    run([cli, "compile", "-b", FQBN, "--build-path", str(build), str(SKETCH)], env)
    print("\nUploading to the ESP32...")
    run([cli, "upload", "-b", FQBN, "-p", port, "--input-dir", str(build),
         str(SKETCH)], env)
    print("\nFlashed. Next:  python tools/device.py setup")


# ==========================================
# WI-FI DETAILS FROM THIS LAPTOP
# ==========================================
def netsh(*args):
    out = subprocess.run(["netsh", "wlan", *args], capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def field(text, name, exact=False):
    m = re.search(rf"^\s*{name}\s*:[ \t]?(.*)$", text, re.M)
    if not m:
        return ""
    # exact=True keeps leading/trailing spaces that are part of the value (SSIDs and
    # passwords may end in a space); only the line ending is removed.
    return m.group(1).rstrip("\r\n") if exact else m.group(1).strip()


def current_wifi_windows():
    info = netsh("show", "interfaces")
    ssid = field(info, "SSID", exact=True)
    profile = field(info, "Profile", exact=True) or ssid
    band = field(info, "Band")
    if not ssid.strip():
        return None
    # netsh pads some lines with an extra trailing space, so try the likely
    # spellings of the profile name until one returns a key.
    password = ""
    for name in dict.fromkeys([profile, ssid, profile[:-1], profile.strip(), ssid.strip()]):
        if name:
            password = field(netsh("show", "profile", f"name={name}", "key=clear"),
                             "Key Content", exact=True)
            if password:
                break
    # Does this network also broadcast on 2.4 GHz (dual-band router)?
    scan = netsh("show", "networks", "mode=bssid")
    bands = set()
    for block in re.split(r"\n(?=SSID \d+ :)", scan):
        if field(block, r"SSID \d+", exact=True) == ssid:
            bands.update(re.findall(r"Band\s*:\s*([\d.]+) GHz", block))
    return {"ssid": ssid, "password": password, "band": band,
            "has_24": "2.4" in bands or band.startswith("2.4")}


def current_wifi_linux():
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "active,ssid,freq", "dev", "wifi"],
                             capture_output=True, text=True).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        parts = line.split(":")
        if parts[0] == "yes":
            ssid, freq = parts[1], parts[-1]
            pw = subprocess.run(["nmcli", "-s", "-g", "802-11-wireless-security.psk",
                                 "connection", "show", ssid],
                                capture_output=True, text=True).stdout.strip()
            ghz = "2.4" if freq.startswith("24") else "5"
            return {"ssid": ssid, "password": pw, "band": f"{ghz} GHz",
                    "has_24": ghz == "2.4"}
    return None


def _run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def mac_wifi_device(hardware_ports):
    """`networksetup -listallhardwareports` -> the Wi-Fi interface (usually en0)."""
    m = re.search(r"Hardware Port:\s*(?:Wi-Fi|AirPort)\s*\nDevice:\s*(\S+)", hardware_ports)
    return m.group(1) if m else "en0"


def mac_ssid(getsummary, airportnetwork):
    """SSID from `ipconfig getsummary <dev>` (macOS 14.4+) or `networksetup
    -getairportnetwork <dev>` (older). Keeps leading/trailing spaces, which are
    part of some names (e.g. phone hotspots). "" if unknown or redacted."""
    m = re.search(r"^\s+SSID : (.*)$", getsummary, re.M)
    ssid = m.group(1).rstrip("\r\n") if m else ""
    if not ssid:
        m = re.search(r"^Current Wi-Fi Network: (.*)$", airportnetwork, re.M)
        ssid = m.group(1).rstrip("\r\n") if m else ""
    # macOS 15 hides the name from apps without Location permission.
    return "" if "<redacted>" in ssid else ssid


def mac_band(system_profiler):
    """Band of the current network from `system_profiler SPAirPortDataType`."""
    current = system_profiler.split("Current Network Information:", 1)
    if len(current) < 2:
        return ""
    m = re.search(r"Channel:\s*\d+\s*\((\d(?:\.\d)?)\s*GHz", current[1])
    return f"{'2.4' if m.group(1).startswith('2') else m.group(1)} GHz" if m else ""


def current_wifi_mac():
    dev = mac_wifi_device(_run(["networksetup", "-listallhardwareports"]))
    ssid = mac_ssid(_run(["ipconfig", "getsummary", dev]),
                    _run(["networksetup", "-getairportnetwork", dev]))
    if not ssid.strip():
        return None  # setup then asks for the name and password
    print("Reading the Wi-Fi password from your Keychain (macOS may ask you to allow it)...")
    password = _run(["security", "find-generic-password", "-D", "AirPort network password",
                     "-a", ssid, "-w"], timeout=120).rstrip("\r\n")
    band = mac_band(_run(["system_profiler", "SPAirPortDataType"], timeout=30))
    return {"ssid": ssid, "password": password, "band": band,
            # Unknown band: don't block, the ESP32 reports it if it can't join.
            "has_24": band.startswith("2.4") or not band}


def current_wifi():
    if IS_WINDOWS:
        return current_wifi_windows()
    if platform.system() == "Darwin":
        return current_wifi_mac()
    if platform.system() == "Linux":
        return current_wifi_linux()
    return None


def server_running():
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=2):
            return True
    except Exception:
        return False


def cmd_setup(args):
    port = find_port(args.port)
    wifi = None if args.ssid else current_wifi()

    if args.ssid:
        ssid = args.ssid
        password = args.password if args.password is not None else input(
            f"Password for \"{ssid}\" (empty if open): ")
    elif wifi:
        ssid = wifi["ssid"]
        print(f"This laptop is on Wi-Fi \"{ssid}\" ({wifi['band'] or 'unknown band'}).")
        if not wifi["has_24"]:
            print("\n  WARNING: this network looks 5 GHz only. The ESP32-S3 can only\n"
                  "  use 2.4 GHz. On a phone hotspot: Android > Hotspot > AP band >\n"
                  "  2.4 GHz; iPhone > Personal Hotspot > Maximize Compatibility.\n"
                  "  Reconnect the laptop afterwards, then run setup again.\n")
            if input("Continue anyway? [y/N] ").strip().lower() != "y":
                return
        password = args.password if args.password is not None else wifi["password"]
        if not password:
            password = input(f"Password for \"{ssid}\" (empty if open): ")
    else:
        ssid = input("Wi-Fi name (2.4 GHz, same network as this laptop): ").strip()
        password = input("Wi-Fi password: ")

    if not server_running():
        print("\nNOTE: server.py isn't running on this laptop yet. Start it in "
              "another window (python server/server.py) so the ESP32 can find it.")

    print(f"\nSending Wi-Fi \"{ssid}\" to the ESP32 on {port}...")
    s = open_serial(port)
    s.reset_input_buffer()
    send(s, "")
    send(s, f"ssid {ssid}")
    send(s, f"pass {password}")
    send(s, f"server {args.server or 'auto'}")
    send(s, "connect")

    def outcome(line):
        if "[server] found" in line:
            return "ok"
        if "[server] not found" in line or "[server] cannot reach" in line:
            return "no-server"
        if "[wifi] FAILED" in line:
            return "no-wifi"
        return None

    result = read_lines(s, 75, outcome)
    s.close()

    print()
    if result == "ok":
        print("SUCCESS - the ESP32 is on Wi-Fi and found the server.")
        print("Speak to it! Watch it with:  python tools/device.py monitor")
    elif result == "no-server":
        print("Wi-Fi OK, but the server wasn't found. Make sure server.py is running,\n"
              "the laptop is on the same Wi-Fi, and allow Python through the\n"
              "Windows Firewall prompt. The ESP32 keeps retrying every 5 s.")
    elif result == "no-wifi":
        print("The ESP32 couldn't join the Wi-Fi. Check the password and that\n"
              "the network is 2.4 GHz.")
    else:
        print("No answer from the ESP32. Is the voice assistant firmware flashed?\n"
              "  python tools/device.py flash")


# ==========================================
# MONITOR / COMMANDS
# ==========================================
def cmd_monitor(args):
    port = find_port(args.port)
    s = open_serial(port)
    print(f"Monitoring {port} - type commands and press Enter, Ctrl+C to quit.")
    import threading

    def typer():
        for line in sys.stdin:
            try:
                send(s, line.rstrip("\r\n"))
            except serial.SerialException:
                pass

    threading.Thread(target=typer, daemon=True).start()
    try:
        read_lines(s, 10 ** 9)
    except KeyboardInterrupt:
        pass
    s.close()


def cmd_calibrate(args):
    port = find_port(args.port)
    s = open_serial(port)
    print("Calibration: stay quiet first, then speak when told to "
          "(the orange LED lights up).\n")

    def outcome(line):
        for word in ("done", "FAILED", "RETRY"):
            if f"[calibrate] {word}" in line:
                return word
        return None

    for attempt in range(3):
        s.reset_input_buffer()
        send(s, "calibrate")
        result = read_lines(s, 30, outcome)
        if result != "RETRY":
            break
        print("\nRunning calibration again with the lower gain...\n")
        time.sleep(1)
    s.close()

    print()
    if result == "done":
        print("Calibrated. Speech quieter than yours (people further away) is "
              "now ignored.\nRe-run this if you move the board or change where "
              "you sit.")
    elif result == "FAILED":
        print("Calibration didn't succeed - follow the hint above and run it again.")
    else:
        print("No answer from the ESP32. Is firmware v2.4+ flashed?")


def cmd_command(args):
    port = find_port(args.port)
    s = open_serial(port)
    s.reset_input_buffer()
    send(s, " ".join(args.words))
    read_lines(s, args.wait)
    s.close()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="serial port, e.g. COM7 or /dev/ttyACM0")
    sub = p.add_subparsers(dest="action", required=True)

    f = sub.add_parser("flash", help="build and upload the firmware")
    f.add_argument("--arduino-data",
                   help="folder for the ESP32 tools (~4 GB), remembered")

    st = sub.add_parser("setup", help="send Wi-Fi settings to the ESP32")
    st.add_argument("--ssid", help="Wi-Fi name (default: this laptop's)")
    st.add_argument("--password", help="Wi-Fi password")
    st.add_argument("--server", help="fixed server address ip[:port] "
                                     "(default: automatic)")

    sub.add_parser("monitor", help="show the live log and type commands")
    sub.add_parser("calibrate", help="learn your voice level (ignores "
                                     "people further away)")

    c = sub.add_parser("cmd", help="run one ESP32 command")
    c.add_argument("words", nargs="+")
    c.add_argument("--wait", type=float, default=8, help="seconds to show output")

    args = p.parse_args()
    {"flash": cmd_flash, "setup": cmd_setup, "monitor": cmd_monitor,
     "calibrate": cmd_calibrate, "cmd": cmd_command}[args.action](args)


if __name__ == "__main__":
    main()
