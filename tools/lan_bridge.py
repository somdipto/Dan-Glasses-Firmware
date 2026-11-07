"""Explicitly approved, isolated RFC1918 HTTPS/WSS fixture; never public ingress.

Prepare creates only a new private directory. Serve replaces this process with a
minimal-environment child, never a daemon. Stop it before removing its directory;
the operator owns cleanup. Existing provider files are read-only references.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.credentials import private_write, registry_read

SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
RFC1918 = tuple(ipaddress.ip_network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
FILES = {"fixture.json", "provider.json", "tls.crt", "tls.key", "device.token", "devices.json"}
DIRECTORIES = {"home", "tmp"}
METADATA_FIELDS = {"agent_id", "agent_version_id", "api_version", "model", "origin", "voice_id"}
MANIFEST_FIELDS = {"version", "bind", "hostname", "port", "provider_key_file", "certificate_sha256"}
DEVICE = "lan-glasses"


def checked_path(value):
    if ".." in Path(value).parts:
        raise ValueError("Parent traversal is not allowed")
    path = Path(os.path.abspath(value))
    # Do not resolve first: that would silently accept a symlink supplied by the operator.
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Symlink paths are not allowed")
    return path


def private_directory(path):
    path = checked_path(path)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()):
        raise ValueError("Directory must be owned by this user with mode 0700")
    return path


def private_file(path, maximum):
    path = checked_path(path)
    private_directory(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > maximum):
            raise ValueError("File must be owned, single-link, bounded and mode 0600")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("File exceeds its bound")
    return data


def json_object(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate metadata field")
            result[key] = value
        return result
    value = json.loads(data, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def provider_metadata(path):
    value = json_object(private_file(path, 4096))
    if set(value) != METADATA_FIELDS or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("Provider metadata must contain exactly the reviewed six string fields")
    patterns = {"agent_id": r"agent_[A-Za-z0-9_-]{1,122}",
                "agent_version_id": r"av_[A-Za-z0-9_-]{1,125}",
                "voice_id": r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"}
    if any(not re.fullmatch(pattern, value[name], re.ASCII) for name, pattern in patterns.items()):
        raise ValueError("Invalid provider metadata identifier")
    if (value["origin"] != "https://api.cartesia.ai" or value["api_version"] != "2026-08-14"
            or value["model"] != "gpt-5.4-mini"):
        raise ValueError("Provider metadata is outside the reviewed Cartesia contract")
    return value


def provider_key(path):
    raw = private_file(path, 4098)
    # Match the existing private_secret convention, without retaining or logging the value.
    key = raw.strip()
    if not 1 <= len(key) <= 4096 or any(c < 33 or c > 126 for c in key):
        raise ValueError("Invalid private provider key file")


def approved_address(approved, bind, hostname):
    if not approved:
        raise ValueError("Explicit --approved-lan is required")
    address = ipaddress.IPv4Address(bind)
    if str(address) != bind or not any(address in network for network in RFC1918):
        raise ValueError("Bind must be an explicit RFC1918 IPv4 address")
    if (not isinstance(hostname, str) or not hostname.isascii() or len(hostname) > 253
            or hostname != hostname.lower() or "." not in hostname
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.ASCII)
                   for label in hostname.split("."))):
        raise ValueError("Hostname must be a strict lower-case ASCII DNS name")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Numeric hostname is not allowed")
    records = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    if not records or {record[4][0] for record in records} != {bind}:
        raise ValueError("DNS must resolve exclusively to the approved bind address")


def available_socket(bind, port):
    if type(port) is not int or (port != 0 and not 1024 <= port <= 65535):
        raise ValueError("Port must be unprivileged and at most 65535")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # No SO_REUSEPORT/REUSEADDR and no alternate port on failure.
        listener.bind((bind, port))
        if listener.getsockname()[1] < 1024:
            raise ValueError("Selected port is privileged")
        return listener
    except BaseException:
        listener.close()
        raise


def openssl(*arguments, data=None):
    binary = shutil.which("openssl", path=SAFE_PATH)
    if not binary:
        raise ValueError("OpenSSL is required")
    result = subprocess.run([binary, *map(str, arguments)], input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=30, check=True,
                            env={"PATH": SAFE_PATH, "LC_ALL": "C"})
    return result.stdout


def tls_context(root, manifest):
    cert = private_file(root / "tls.crt", 3999)
    private_file(root / "tls.key", 8192)
    if hashlib.sha256(cert).hexdigest() != manifest["certificate_sha256"]:
        raise ValueError("Fixture certificate changed")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(root / "tls.crt", root / "tls.key")
    # Verify the real TLS handshake, including dates, trust and DNS identity.
    # macOS LibreSSL's verify command does not support -verify_hostname.
    client_context = ssl.create_default_context(cafile=str(root / "tls.crt"))
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    endpoints = (
        (client_context.wrap_bio(client_in, client_out, server_hostname=manifest["hostname"]), client_out, server_in),
        (context.wrap_bio(server_in, server_out, server_side=True), server_out, client_in),
    )
    complete = [False, False]
    for _ in range(16):
        for index, (endpoint, outgoing, incoming) in enumerate(endpoints):
            if not complete[index]:
                try:
                    endpoint.do_handshake()
                    complete[index] = True
                except ssl.SSLWantReadError:
                    pass
            data = outgoing.read()
            if len(data) > 16384:
                raise ValueError("Unexpected certificate handshake size")
            if data:
                incoming.write(data)
        if all(complete):
            return context
    raise ValueError("Fixture TLS identity verification did not complete")


def prepare(args):
    approved_address(args.approved_lan, args.bind, args.hostname)
    root = checked_path(args.directory)
    private_directory(root.parent)
    if root.exists():
        raise ValueError("Fixture directory must not already exist")
    metadata = provider_metadata(args.provider_metadata_file)
    key_file = checked_path(args.provider_key_file)
    provider_key(key_file)
    # All validation precedes the first fixture write, including local ownership of the IP.
    with available_socket(args.bind, args.port) as selected:
        port = selected.getsockname()[1]
        root.mkdir(mode=0o700)
        for name in DIRECTORIES:
            (root / name).mkdir(mode=0o700)
        for name in ("tls.key", "tls.crt"):
            private_write(root / name, "")
        config = ("[req]\nprompt = no\ndistinguished_name = dn\nx509_extensions = server\n"
                  f"[dn]\nCN = {args.hostname}\n[server]\n"
                  "basicConstraints = critical,CA:TRUE,pathlen:0\n"
                  "keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign\n"
                  "subjectKeyIdentifier = hash\n"
                  "authorityKeyIdentifier = keyid:always,issuer\n"
                  "extendedKeyUsage = serverAuth\n"
                  f"subjectAltName = DNS:{args.hostname}\n")
        openssl("req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "30",
                "-keyout", root / "tls.key", "-out", root / "tls.crt", "-config", "/dev/stdin",
                data=config.encode("ascii"))
        certificate = private_file(root / "tls.crt", 3999)
        token = secrets.token_urlsafe(32)
        private_write(root / "device.token", token + "\n")
        private_write(root / "devices.json", json.dumps({DEVICE: hashlib.sha256(token.encode("ascii")).hexdigest()}) + "\n")
        private_write(root / "provider.json", json.dumps(metadata, indent=2) + "\n")
        manifest = {"version": 1, "bind": args.bind, "hostname": args.hostname, "port": port,
                    "provider_key_file": str(key_file), "certificate_sha256": hashlib.sha256(certificate).hexdigest()}
        tls_context(root, manifest)
        # Last write is the completion record. Partial output is intentionally never deleted/reused.
        private_write(root / "fixture.json", json.dumps(manifest, indent=2) + "\n")
    return root, manifest


def review(directory, approved):
    if not approved:
        raise ValueError("Explicit --approved-lan is required")
    root = private_directory(directory)
    if {entry.name for entry in root.iterdir()} != FILES | DIRECTORIES:
        raise ValueError("Unknown or incomplete fixture directory data")
    for name in DIRECTORIES:
        private_directory(root / name)
    manifest = json_object(private_file(root / "fixture.json", 4096))
    if (set(manifest) != MANIFEST_FIELDS or type(manifest["version"]) is not int or manifest["version"] != 1
            or not isinstance(manifest["certificate_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", manifest["certificate_sha256"])
            or not isinstance(manifest["provider_key_file"], str)
            or not Path(manifest["provider_key_file"]).is_absolute()):
        raise ValueError("Unrecognized fixture manifest")
    approved_address(approved, manifest["bind"], manifest["hostname"])
    if type(manifest["port"]) is not int or not 1024 <= manifest["port"] <= 65535:
        raise ValueError("Invalid persisted port")
    metadata = provider_metadata(root / "provider.json")
    provider_key(manifest["provider_key_file"])
    token = private_file(root / "device.token", 44)
    if not re.fullmatch(rb"[A-Za-z0-9_-]{43}\n", token):
        raise ValueError("Invalid fixture device token")
    private_file(root / "devices.json", 1024)
    devices = registry_read(root / "devices.json")
    expected = {DEVICE: hashlib.sha256(token.rstrip(b"\n")).hexdigest()}
    if devices != expected:
        raise ValueError("Fixture registry does not match its device token")
    return root, manifest, metadata, tls_context(root, manifest)


def child_environment(root, manifest, metadata):
    return {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "PYTHONUTF8": "1", "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(root / "home"), "XDG_CACHE_HOME": str(root / "home"),
            "VOICE_BACKEND": "cartesia-stream", "BRIDGE_DEVICES_FILE": str(root / "devices.json"),
            "BRIDGE_TLS_CERT": str(root / "tls.crt"), "BRIDGE_TLS_KEY": str(root / "tls.key"),
            "CARTESIA_BASE_URL": metadata["origin"], "CARTESIA_KEY_FILE": manifest["provider_key_file"],
            "CARTESIA_AGENT_ID": metadata["agent_id"], "CARTESIA_AGENT_VERSION_ID": metadata["agent_version_id"],
            "CARTESIA_VERSION": metadata["api_version"]}


def origin(manifest):
    return f'https://{manifest["hostname"]}:{manifest["port"]}'


async def run_site(app, listener, context, ready):
    """Start the real app on an already reserved socket; stop and join on signals."""
    from aiohttp import web
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=2)
    try:
        await runner.setup()
        site = web.SockSite(runner, listener, ssl_context=context, shutdown_timeout=2)
        await site.start()
        ready()
        await stop.wait()
    finally:
        await runner.cleanup()
        listener.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def serve(args, *, child=False):
    root, manifest, metadata, context = review(args.directory, args.approved_lan)
    if not child:
        # Check the persisted port before exec; child reserves that same port, never port zero.
        with available_socket(manifest["bind"], manifest["port"]):
            pass
        environment = child_environment(root, manifest, metadata)
        os.chdir(root)
        os.execve(sys.executable, [sys.executable, "-B", str(Path(__file__).resolve()), "_run",
                                 "--approved-lan", "--directory", str(root)], environment)
        raise RuntimeError("execve unexpectedly returned")
    # Clear even a directly invoked internal child; inherited proxies/provider settings never apply.
    os.environ.clear()
    os.environ.update(child_environment(root, manifest, metadata))
    logging.disable(logging.CRITICAL)
    # These private directories belong only to this fixture, not the user's
    # normal HOME. Keep their contents across an unclean stop; never delete
    # arbitrary remnants or make a valid TLS/device identity unstartable.
    from server.server import Settings, load_devices
    from server.turns import configured_backend
    from server.streaming import create_stream_app
    load_devices(root / "devices.json")
    app = create_stream_app(Settings.from_env(), configured_backend())
    listener = available_socket(manifest["bind"], manifest["port"])
    listener.setblocking(False)
    asyncio.run(run_site(app, listener, context,
                         lambda: print("VOICE_LAN_READY " + origin(manifest), flush=True)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "serve", "_run"):
        command = commands.add_parser(name, help=argparse.SUPPRESS if name == "_run" else name)
        command.add_argument("--approved-lan", action="store_true")
        command.add_argument("--directory", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--bind", required=True)
            command.add_argument("--hostname", required=True)
            command.add_argument("--port", type=int, default=0)
            command.add_argument("--provider-metadata-file", type=Path, required=True)
            command.add_argument("--provider-key-file", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "prepare":
            root, manifest = prepare(args)
            print("VOICE_LAN_PREPARED " + origin(manifest))
            print("CA_FILE=" + str(root / "tls.crt"))
            print("TOKEN_FILE=" + str(root / "device.token"))
        else:
            serve(args, child=args.command == "_run")
    except KeyboardInterrupt:
        pass
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, subprocess.SubprocessError):
        parser.exit(1, "LAN fixture operation refused/failed; check approval, private paths, DNS, port and TLS. No secrets printed.\n")


if __name__ == "__main__":
    main()
