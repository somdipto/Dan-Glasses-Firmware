"""Pinned firmware builds and separately approved device writes.

Provisioning keeps one USB connection through a fresh readiness handshake.
Unconfigured devices support first pairing; configured devices authenticate
USB maintenance with their current device token. Wi-Fi input stays hidden.
Build receipt schema 3 binds the current firmware contract and hashed inputs;
older receipts must be rebuilt, never relabeled.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from .credentials import hidden_input
    from .commissioning import FIRMWARE, CommissionError, initial_readiness, exchange as status_exchange, parse_status
except ImportError:
    from credentials import hidden_input
    from commissioning import FIRMWARE, CommissionError, initial_readiness, exchange as status_exchange, parse_status

ROOT = Path(__file__).resolve().parent.parent
CLI_VERSION = "1.5.1"
CORE_VERSION = "3.3.10"
WEBSOCKETS_VERSION = "2.7.1"
WEBSOCKETS_COMMIT = "3af6593a00bd4dc8b18be5082575618cf2acc44d"
# SHA256 of sorted path + NUL + file SHA256 + LF for library.properties and
# every src/ file in the audited upstream commit (not a version-label trust).
WEBSOCKETS_SOURCES_SHA256 = "f97bf5299e4258a559e8c4ecd43afe8c1e2c9439d5db0f45abe5bc72625cfb7f"
BUILD_PROPERTY = "compiler.cpp.extra_flags=-DNODEBUG_WEBSOCKETS"
FQBN = ("esp32:esp32:XIAO_ESP32S3:PSRAM=opi,FlashSize=8M,USBMode=hwcdc,"
        "CDCOnBoot=default,PartitionScheme=default_8MB,DebugLevel=none")
BOARD = "Seeed XIAO ESP32-S3 (regular, non-Plus; 8 MB flash, OPI PSRAM)"
MANIFEST = "device-build.json"
UPLOAD_BINARIES = ("voice_assistant.ino.bootloader.bin", "voice_assistant.ino.partitions.bin",
                   "voice_assistant.ino.bin")


def doctor():
    from serial.tools import list_ports
    ports = list(list_ports.comports())
    for port in ports:
        identity = f"VID={port.vid:04x} PID={port.pid:04x}" if port.vid is not None and port.pid is not None else "no USB VID/PID"
        print(f"{port.device}: {identity}; {port.description}")
    usb = [p for p in ports if p.vid is not None]
    print(f"Read-only inventory: {len(ports)} serial entries, {len(usb)} USB-identified entries.")
    print("No serial port opened; no reset, boot command, chip probe, flash or erase.")
    print("USB identity cannot confirm board model, chip memory, wiring or electrical safety.")


def pinned_cli():
    cli = shutil.which("arduino-cli")
    if not cli:
        raise ValueError("Install arduino-cli 1.5.1 explicitly; this tool does not install or upgrade anything")
    version = json.loads(subprocess.check_output([cli, "version", "--format", "json"]))
    if version.get("VersionString") != CLI_VERSION:
        raise ValueError("Expected arduino-cli 1.5.1")
    cores = json.loads(subprocess.check_output([cli, "core", "list", "--format", "json"]))
    if not any(p.get("id") == "esp32:esp32" and p.get("installed_version") == CORE_VERSION for p in cores.get("platforms", [])):
        raise ValueError("Install esp32:esp32@3.3.10 explicitly; no automatic global toolchain changes")
    return cli


def active_platform(cli):
    details = json.loads(subprocess.check_output(
        [cli, "board", "details", "--fqbn", FQBN, "--format", "json"]))
    properties = dict(item.split("=", 1) for item in details.get("build_properties", []) if "=" in item)
    platform = properties.get("runtime.platform.path")
    if details.get("version") != CORE_VERSION or not platform:
        raise ValueError("Cannot confirm the active pinned ESP32 platform; no upload started")
    return platform


def file_hash(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Build inputs must be regular files, not symlinks")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def firmware_sources():
    sketch = ROOT / "firmware" / "voice_assistant"
    if sketch.is_symlink():
        raise ValueError("Firmware source directory must not be a symlink")
    sources = {}
    for path in sorted(sketch.rglob("*")):
        if path.is_symlink():
            raise ValueError("Firmware sources must not contain symlinks")
        if path.is_file():
            sources[path.relative_to(sketch).as_posix()] = file_hash(path)
    if "voice_assistant.ino" not in sources:
        raise ValueError("Firmware sketch is missing")
    return sources


def websocket_receipt(library):
    library = Path(library)
    if not library.is_absolute() or any(path.is_symlink() for path in (library, *library.parents)):
        raise ValueError("WebSockets library must have an absolute, non-symlinked install path")
    source = library / "src"
    if source.is_symlink() or not source.is_dir():
        raise ValueError("WebSockets source directory missing or symlinked")
    files = {"library.properties": file_hash(library / "library.properties")}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("WebSockets sources must not contain symlinks")
        if path.is_file():
            files[path.relative_to(library).as_posix()] = file_hash(path)
    properties = (library / "library.properties").read_text()
    if not re.search(r"(?m)^version=" + re.escape(WEBSOCKETS_VERSION) + r"$", properties):
        raise ValueError("Install the explicitly reviewed WebSockets@2.7.1; no automatic dependency changes")
    digest = hashlib.sha256("".join(name + "\0" + files[name] + "\n"
                                    for name in sorted(files)).encode()).hexdigest()
    if digest != WEBSOCKETS_SOURCES_SHA256:
        raise ValueError("WebSockets sources differ from the audited upstream commit; no build/upload permitted")
    return {"path": str(library), "version": WEBSOCKETS_VERSION, "commit": WEBSOCKETS_COMMIT,
            "sources_sha256": digest, "files": files}


def installed_websocket_library(cli):
    listing = json.loads(subprocess.check_output([cli, "lib", "list", "--format", "json"]))
    matches = [item["library"] for item in listing.get("installed_libraries", [])
               if item.get("library", {}).get("name") == "WebSockets"]
    if len(matches) != 1 or matches[0].get("version") != WEBSOCKETS_VERSION:
        raise ValueError("Explicitly install WebSockets@2.7.1 first; this tool never installs libraries")
    receipt = websocket_receipt(matches[0].get("install_dir", ""))
    return receipt


def compiled_websocket_receipt(output):
    entries = json.loads((output / "compile_commands.json").read_text())
    clients = [Path(entry["file"]) for entry in entries
               if Path(entry.get("file", "")).name == "WebSocketsClient.cpp"]
    if len(clients) != 1 or clients[0].parent.name != "src":
        raise ValueError("Compile database must identify exactly one reviewed WebSocketsClient source")
    receipt = websocket_receipt(clients[0].parent.parent)
    source = Path(receipt["path"]) / "src"
    expected = {str(source / name.removeprefix("src/")) for name in receipt["files"]
                if name.startswith("src/") and Path(name).suffix in (".c", ".cpp")}
    compiled = set()
    for entry in entries:
        path = Path(entry.get("file", ""))
        if path.is_relative_to(source):
            compiled.add(str(path))
            if path.suffix == ".cpp" and "-DNODEBUG_WEBSOCKETS" not in entry.get("arguments", []):
                raise ValueError("WebSockets C++ sources must disable credential-bearing debug logging")
    if compiled != expected:
        raise ValueError("Compiled WebSockets sources differ from the reviewed dependency")
    return receipt




def reviewed_output():
    output = ROOT / ".runtime" / "xiao-reviewed-build"
    if output.parent.is_symlink() or output.is_symlink():
        raise ValueError("Reviewed build directory must not be a symlink")
    return output


def build_metadata(output):
    options = json.loads((output / "build.options.json").read_text())
    if options.get("fqbn") != FQBN or options.get("customBuildProperties") != BUILD_PROPERTY:
        raise ValueError("Build target/options do not match the reviewed XIAO target")
    platforms = set(options.get("hardwareFolders", "").split(","))
    if len(platforms) != 1:
        raise ValueError("Expected one pinned ESP32 platform directory")
    platform = Path(platforms.pop())
    if not platform.is_absolute() or platform.parts[-4:] != ("esp32", "hardware", "esp32", CORE_VERSION):
        raise ValueError("Build platform does not match esp32:esp32@3.3.10")
    if any((platform / name).exists() for name in ("platform.local.txt", "boards.local.txt")):
        raise ValueError("Local ESP32 platform/board overrides are not part of the reviewed toolchain")
    return {
        "schema": 3,
        "firmware": FIRMWARE,
        "reviewed_actual_board": True,
        "board": BOARD,
        "fqbn": FQBN,
        "arduino_cli": CLI_VERSION,
        "esp32_core": CORE_VERSION,
        "sources": firmware_sources(),
        "websocket_library": compiled_websocket_receipt(output),
        "artifacts": {name: file_hash(output / name) for name in sorted(
            set(UPLOAD_BINARIES) | {path.name for path in output.glob("*.bin")} |
            {"build.options.json", "compile_commands.json"})},
        # The pinned upload recipe reads boot_app0 from the installed core, not its build copy.
        "platform": str(platform),
        "platform_inputs": {name: file_hash(platform / name) for name in
                            ("platform.txt", "boards.txt", "tools/flasher.py", "tools/partitions/boot_app0.bin")},
    }


def build(args):
    if args.reference_board == args.actual_board:
        raise ValueError("Select exactly one of --reference-board or --actual-board")
    if args.actual_board and not args.hardware_confirmed:
        raise ValueError("Actual-board build requires --hardware-confirmed after memory, wiring and electrical review")
    cli = pinned_cli()
    output = reviewed_output() if args.actual_board else ROOT / ".runtime" / "reference-build"
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / MANIFEST
    manifest.unlink(missing_ok=True)
    sources = firmware_sources()
    dependency = installed_websocket_library(cli)
    command = [cli, "compile", "--fqbn", FQBN, "--warnings", "all", "--build-path", str(output),
               "--build-property", BUILD_PROPERTY, "--library", dependency["path"],
               str(ROOT / "firmware" / "voice_assistant")]
    if args.actual_board:
        command.insert(2, "--clean")
    print("Building reviewed regular XIAO ESP32-S3." if args.actual_board else
          "Building XIAO ESP32-S3 reference only: 8 MB flash, OPI PSRAM. Actual hardware NOT confirmed.", flush=True)
    subprocess.run(command, check=True)
    if args.actual_board:
        metadata = build_metadata(output)
        if metadata["platform"] != active_platform(cli):
            raise ValueError("Compile platform differs from the active toolchain; no reviewed manifest written")
        if metadata["sources"] != sources:
            raise ValueError("Firmware changed during compile; no reviewed manifest written")
        if metadata["websocket_library"] != dependency:
            raise ValueError("WebSockets dependency changed during compile; no reviewed manifest written")
        manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    print("Build complete. No port opened or firmware uploaded; flash requires separate explicit approval.")


def flash(args):
    if not args.approved_flash or not args.hardware_confirmed:
        raise ValueError("Flash requires --approved-flash and --hardware-confirmed; no upload started")
    if not args.port or args.port.startswith("-") or any(ord(c) <= 32 or ord(c) == 127 for c in args.port):
        raise ValueError("An explicit serial port is required; no upload started")
    output = reviewed_output()
    manifest = output / MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("Reviewed actual-board build manifest missing; reference builds cannot be flashed")
    try:
        recorded = json.loads(manifest.read_text())
        current = build_metadata(output)
    except (OSError, ValueError, TypeError, AttributeError):
        raise ValueError("Reviewed build inputs missing or invalid; rebuild before flashing") from None
    if recorded != current:
        raise ValueError("Reviewed build is stale or changed; rebuild before flashing")
    cli = pinned_cli()
    if recorded["platform"] != active_platform(cli):
        raise ValueError("Active ESP32 platform changed; rebuild before flashing")
    # Wait for metadata for this explicit port; never auto-pick a port or retry writes.
    subprocess.run([cli, "upload", "--discovery-timeout", "15s", "--fqbn", FQBN,
                    "--port", args.port, "--protocol", "serial",
                    "--input-dir", str(output), str(ROOT / "firmware" / "voice_assistant")], check=True)
    print("Upload command completed. Startup is hands-free after Wi-Fi/TLS readiness; on-device operation remains unverified.")


def validate_bridge_origin(origin):
    # Match the firmware's raw ASCII grammar; URL parsers silently strip CR/LF/TAB.
    match = re.fullmatch(r"https://([A-Za-z0-9.-]{1,253})(?::([0-9]{1,5}))?/?", origin)
    if not match:
        raise ValueError("An explicit HTTPS DNS origin is required")
    host, port = match.groups()
    if (not re.search(r"[A-Za-z]", host)
            or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                   for label in host.split("."))
            or (port is not None and not 1 <= int(port) <= 65535)):
        raise ValueError("An explicit HTTPS DNS origin is required")


def private_token(path):
    if path.is_symlink() or not path.is_file() or (os.name == "posix" and path.stat().st_mode & 0o077):
        raise ValueError("Device token file must be private, mode 0600")
    if path.stat().st_size > 128:
        raise ValueError("Device token file is too large")
    token = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise ValueError("Invalid device token")
    return token


def receive(port, seconds):
    end = time.monotonic() + seconds
    data = bytearray()
    while time.monotonic() < end and len(data) < 8192:
        data.extend(port.read(256))
    return data.decode("utf-8", "replace")


def provision(args):
    if not args.approved_device_write or not args.hardware_confirmed:
        raise ValueError("Provisioning writes NVS. Requires separate approval and confirmed safe hardware; no port opened.")
    validate_bridge_origin(args.bridge_url)
    token = private_token(args.token_file)
    pem = args.ca_file.read_bytes()
    if not 100 <= len(pem) <= 3999 or b"-----BEGIN CERTIFICATE-----" not in pem:
        raise ValueError("Provide the reviewed bridge CA certificate, at most 3999 bytes")
    import serial
    port = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=3)
    port.port, port.dtr, port.rts = args.port, False, False
    with port:
        try:
            initial_readiness(port)
            status = parse_status(status_exchange(port, "status"))
            if not status["usb_receive_ready"]:
                raise ValueError("USB receive capacity unavailable; no credential sent")
            if not status["usb_unlocked"]:
                pairing = ("pair " + token + "\n").encode("ascii") if status["bridge_configured"] else b"pair\n"
                if port.write(pairing) != len(pairing):
                    raise ValueError("USB pairing command incomplete; no Wi-Fi credential sent")
                receive(port, 1)
                status = parse_status(status_exchange(port, "status"))
        except CommissionError:
            raise ValueError("Current secure firmware and fresh USB unlock required; no credential sent") from None
        if not status["usb_receive_ready"] or not status["usb_unlocked"] or status["listening"]:
            raise ValueError("Current secure firmware, paused listening and USB unlock required; no credential sent")
        ssid = hidden_input("2.4 GHz hotspot name (hidden): ")
        password = hidden_input("Hotspot WPA password (hidden): ")
        if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(password.encode()) <= 63 or any(c in ssid + password for c in "\r\n\0"):
            raise ValueError("Invalid Wi-Fi credentials; no credential sent")
        commands = [("ssid " + ssid, "network name saved"), ("pass " + password, "password saved"),
                    ("bridge " + args.bridge_url, "bridge origin saved"),
                    ("ca " + base64.b64encode(pem).decode("ascii"), "trust anchor saved"),
                    ("token " + token, "device credential saved")]
        for command, expected in commands:
            port.write((command + "\n").encode("utf-8"))
            port.flush()
            reply = receive(port, 1)
            if reply.splitlines().count("[ok] " + expected) != 1:
                raise ValueError("Provisioning rejected; no raw serial output printed")
    print("Credentials stored; listening remains paused. No voice/provider/cloud call was made by this tool.")
    print("Connection/audio diagnostics require the next separately approved hardware step.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="enumerate OS serial metadata only")
    build_parser = commands.add_parser("build", help="compile only; reference or hardware-reviewed regular XIAO")
    targets = build_parser.add_mutually_exclusive_group(required=True)
    targets.add_argument("--reference-board", action="store_true")
    targets.add_argument("--actual-board", action="store_true")
    build_parser.add_argument("--hardware-confirmed", action="store_true",
                              help="attest actual memory, wiring, power and electrical safety are reviewed")
    upload = commands.add_parser("flash", help="separately approved upload of a current reviewed actual-board build")
    upload.add_argument("--port", required=True)
    upload.add_argument("--approved-flash", action="store_true")
    upload.add_argument("--hardware-confirmed", action="store_true",
                        help="attest actual memory, wiring, power and electrical safety are reviewed")
    device = commands.add_parser("provision", help="approval-gated future USB credential write; NOT run during preparation")
    device.add_argument("--port", required=True)
    device.add_argument("--bridge-url", required=True)
    device.add_argument("--token-file", type=Path, required=True)
    device.add_argument("--ca-file", type=Path, required=True)
    device.add_argument("--approved-device-write", action="store_true")
    device.add_argument("--hardware-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            doctor()
        elif args.command == "build":
            build(args)
        elif args.command == "flash":
            flash(args)
        else:
            provision(args)
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    except (OSError, subprocess.SubprocessError):
        parser.exit(1, "Operation failed. No credential, raw device log or insecure fallback printed.\n")


if __name__ == "__main__":
    main()
