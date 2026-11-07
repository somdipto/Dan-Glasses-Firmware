"""Create local secret files without putting credentials in arguments or output."""
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path


def hidden_input(prompt):
    if not sys.stdin.isatty():
        raise ValueError("Hidden credential entry requires an interactive terminal")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except (getpass.GetPassWarning, EOFError):
            raise ValueError("Cannot disable terminal echo; no credential accepted") from None


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(data)


def registry_read(path):
    if not path.exists():
        return {}
    if path.is_symlink() or (os.name == "posix" and path.stat().st_mode & 0o077):
        raise ValueError("Registry must be a private regular file")
    if path.stat().st_size > 32768:
        raise ValueError("Registry is too large")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Invalid registry")
    return result


def private_replace(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".credential-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(".secrets"))
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create-device", "revoke-device", "rotate-device"):
        commands.add_parser(command).add_argument("device")
    commands.add_parser("store").add_argument("name", choices=("cartesia", "llm", "search"))
    commands.add_parser("local-tls")
    args = parser.parse_args()
    directory = args.directory
    if directory.is_symlink():
        parser.error("Secret directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and directory.stat().st_mode & 0o077:
        parser.error("Secret directory must have mode 0700")
    try:
        if args.command in ("create-device", "revoke-device", "rotate-device"):
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.device):
                raise ValueError("Device ID must use 1 to 64 ASCII letters, digits, underscores or hyphens")
            registry = directory / "devices.json"
            devices = registry_read(registry)
            if args.command != "revoke-device":
                if args.command == "create-device" and args.device in devices:
                    raise ValueError("Device already exists; use rotate-device to revoke and replace its token")
                if args.device not in devices and len(devices) >= 128:
                    raise ValueError("Maximum 128 devices")
                token = secrets.token_urlsafe(32)
                path = directory / f"{args.device}.token"
                if args.command == "rotate-device":
                    if not path.is_file() or path.is_symlink():
                        raise ValueError("Rotation requires an existing private device token file")
                    private_replace(path, token + "\n")
                else:
                    private_write(path, token + "\n")
                devices[args.device] = hashlib.sha256(token.encode("ascii")).hexdigest()
            else:
                if args.device not in devices:
                    raise ValueError("Device is not registered")
                del devices[args.device]
            private_replace(registry, json.dumps(devices, indent=2) + "\n")
            print("Device registry updated. No credential was printed.")
        elif args.command == "store":
            secret = hidden_input(f"{args.name} credential (hidden): ")
            if not secret or len(secret) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
                raise ValueError("Credential must be 1-4096 visible ASCII characters")
            private_write(directory / f"{args.name}.key", secret + "\n")
            print("Credential stored locally. No service configuration was changed.")
        else:
            cert, key = directory / "localhost.crt", directory / "localhost.key"
            if cert.exists() or key.exists():
                raise ValueError("Local TLS files already exist")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "7", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
            key.chmod(0o600)
            cert.chmod(0o600)
            print("Created seven-day localhost-only TLS files. Not for VM deployment or board provisioning.")
    except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
        parser.exit(1, "Credential operation failed. Check file permissions, existing files and input. No secret printed.\n")


if __name__ == "__main__":
    main()
