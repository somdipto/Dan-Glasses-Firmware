"""Offline boundaries and actual local TLS/health, never provider or LAN traffic.

Run: python -m unittest discover -s tests -p test_lan_bridge.py -v
Only address discovery is substituted: the fixture still generates real OpenSSL
keys/certificates, uses real file permissions, and serves the actual aiohttp app.
"""
import argparse
import asyncio
import hashlib
import json
import http.client
import os
from pathlib import Path
import socket
import ssl
import selectors
import subprocess
import sys
import textwrap
import tempfile
import unittest
from unittest.mock import patch

from tools import lan_bridge as lan
from tools.credentials import private_write

BIND = "10.20.30.40"
HOST = "lan-fixture.example.invalid"
METADATA = {"agent_id": "agent_offline", "agent_version_id": "av_offline",
            "api_version": "2026-08-14", "model": "gpt-5.4-mini",
            "origin": "https://api.cartesia.ai", "voice_id": "00000000-0000-0000-0000-000000000000"}
ROOT = Path(__file__).resolve().parents[1]


def records(address=BIND):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))]


class FixtureMixin:
    def make_home(self):
        # macOS /var can be a symlink: use the canonical temporary parent explicitly.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.home.chmod(0o700)
        self.metadata = self.home / "source.json"
        self.key = self.home / "source.key"
        private_write(self.metadata, json.dumps(METADATA))
        private_write(self.key, "offline-not-a-real-provider-key\n")
        self.args = argparse.Namespace(approved_lan=True, bind=BIND, hostname=HOST, port=0,
                                       directory=self.home / "fixture", provider_metadata_file=self.metadata,
                                       provider_key_file=self.key)

    def prepare_fixture(self):
        real_socket = lan.available_socket
        # There is no RFC1918 interface requirement for the offline test suite.
        with patch.object(lan.socket, "getaddrinfo", return_value=records()), \
                patch.object(lan, "available_socket", side_effect=lambda bind, port: real_socket("127.0.0.1", port)):
            return lan.prepare(self.args)

    def review_fixture(self):
        with patch.object(lan.socket, "getaddrinfo", return_value=records()):
            return lan.review(self.args.directory, True)


class LANBoundaries(FixtureMixin, unittest.TestCase):
    def setUp(self):
        self.make_home()

    def test_approval_precedes_dns_and_writes(self):
        self.args.approved_lan = False
        with patch.object(lan.socket, "getaddrinfo") as dns, self.assertRaises(ValueError):
            lan.prepare(self.args)
        dns.assert_not_called()
        self.assertFalse(self.args.directory.exists())
        with self.assertRaises(ValueError):
            lan.review(self.home, False)

    def test_only_rfc1918_and_strict_dns_names(self):
        invalid_addresses = ("0.0.0.0", "127.0.0.1", "8.8.8.8", "100.64.0.1", "169.254.1.1", "::1", "10.01.2.3")
        invalid_names = ("10.20.30.40", "http://fixture.test", "https://fixture.test", "fixture.test/path",
                         "fixture.test\n", "fixture.test\x00", "fíxture.test", "-bad.test", "bad-.test",
                         "bad..test", "bad.test.", "bad_test.test", "localhost", "UPPER.test", "a" * 64 + ".test")
        for bind in invalid_addresses:
            with self.subTest(bind=bind), self.assertRaises(ValueError):
                lan.approved_address(True, bind, HOST)
        for hostname in invalid_names:
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                lan.approved_address(True, BIND, hostname)
        for bind in ("10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.0.1"):
            with patch.object(lan.socket, "getaddrinfo", return_value=records(bind)):
                lan.approved_address(True, bind, HOST)

    def test_dns_rejects_missing_mixed_or_alternate_answers(self):
        for answers in ([], records("10.20.30.41"), records() + records("::1"), records() + records("8.8.8.8")):
            with patch.object(lan.socket, "getaddrinfo", return_value=answers), self.assertRaises(ValueError):
                lan.approved_address(True, BIND, HOST)

    def test_no_overwrite_private_parent_and_symlinks(self):
        self.args.directory.mkdir(mode=0o700)
        with patch.object(lan.socket, "getaddrinfo", return_value=records()), self.assertRaises(ValueError):
            lan.prepare(self.args)
        self.args.directory.rmdir()
        self.home.chmod(0o755)
        with patch.object(lan.socket, "getaddrinfo", return_value=records()), self.assertRaises(ValueError):
            lan.prepare(self.args)
        self.home.chmod(0o700)
        link = self.home / "alias"
        link.symlink_to(self.home, target_is_directory=True)
        self.args.directory = link / "fixture"
        with patch.object(lan.socket, "getaddrinfo", return_value=records()), self.assertRaises(ValueError):
            lan.prepare(self.args)
        with self.assertRaises(ValueError):
            lan.checked_path(str(link) + "/../fixture")
        self.assertFalse((self.home / "fixture").exists())

    def test_metadata_and_key_file_boundaries(self):
        for field, value in (("origin", "https://attacker.test"), ("api_version", "2026-01-01"),
                             ("model", "other"), ("agent_id", "agent_x\n"), ("voice_id", "invalid"),
                             ("agent_version_id", None), ("unknown", "extra")):
            data = dict(METADATA, **{field: value})
            self.metadata.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(ValueError):
                lan.provider_metadata(self.metadata)
        self.metadata.write_text('{"agent_id":"first","agent_id":"second"}')
        with self.assertRaises(ValueError):
            lan.provider_metadata(self.metadata)
        for data in (b"", b"x" * 4099, b"hello\x00world", b"non ascii \xff"):
            self.key.write_bytes(data)
            with self.assertRaises(ValueError):
                lan.provider_key(self.key)
        self.key.write_text("offline-key\n")
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            lan.provider_key(self.key)
        self.key.chmod(0o600)
        alias = self.home / "key-alias"
        alias.symlink_to(self.key)
        with self.assertRaises(ValueError):
            lan.provider_key(alias)
        os.link(self.key, self.home / "hardlink")
        with self.assertRaises(ValueError):
            lan.provider_key(self.key)

    def test_certificate_token_permissions_and_environment_privacy(self):
        root, initial = self.prepare_fixture()
        before = {name: (root / name).read_bytes() for name in lan.FILES}
        self.assertLessEqual(len(before["tls.crt"]), 3999)
        self.assertRegex(before["device.token"], rb"^[A-Za-z0-9_-]{43}\n$")
        self.assertEqual(json.loads(before["devices.json"]),
                         {lan.DEVICE: hashlib.sha256(before["device.token"].strip()).hexdigest()})
        for name in lan.FILES:
            self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
        for name in lan.DIRECTORIES:
            self.assertEqual((root / name).stat().st_mode & 0o777, 0o700)
        environment = lan.child_environment(root, initial, METADATA)
        self.assertEqual(environment["VOICE_BACKEND"], "cartesia-stream")
        self.assertEqual(environment["CARTESIA_KEY_FILE"], str(self.key))
        self.assertNotIn(self.key.read_text().strip(), json.dumps(environment))
        self.assertFalse(any(key in environment for key in ("HTTP_PROXY", "HTTPS_PROXY", "PYTHONPATH", "LLM_API_KEY")))

    @unittest.skipUnless(os.name == "posix", "POSIX unclean process termination")
    def test_real_tls_server_restarts_after_unclean_stop_without_changing_identity(self):
        root, manifest = self.prepare_fixture()
        before = {name: (root / name).read_bytes() for name in lan.FILES}
        leftover = root / "home" / "unclean-run"
        leftover.mkdir(mode=0o700)
        private_write(leftover / "marker", "preserve")
        child_code = textwrap.dedent("""
            import argparse, sys
            from pathlib import Path
            from unittest.mock import patch
            from tools import lan_bridge as lan
            original = lan.available_socket
            with patch.object(lan, 'approved_address'), patch.object(lan, 'available_socket', side_effect=lambda bind, port: original('127.0.0.1', port)):
                lan.serve(argparse.Namespace(directory=Path(sys.argv[1]), approved_lan=True), child=True)
        """)
        context = ssl.create_default_context(cafile=str(root / "tls.crt"))
        token = (root / "device.token").read_text().strip()
        for attempt in range(2):
            process = subprocess.Popen(
                [sys.executable, "-B", "-c", child_code, str(root)], cwd=ROOT,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home),
                     "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(10), "Isolated TLS server did not become ready")
                self.assertTrue(process.stdout.readline(1024).startswith(b"VOICE_LAN_READY "))
                with socket.create_connection(("127.0.0.1", manifest["port"]), timeout=3) as raw:
                    with context.wrap_socket(raw, server_hostname=HOST) as connection:
                        connection.sendall(
                            f"GET /health HTTP/1.1\r\nHost: {HOST}\r\nAuthorization: Bearer {token}\r\nConnection: close\r\n\r\n".encode("ascii"))
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(response.read(513))["status"], "ok")
                self.assertEqual({name: (root / name).read_bytes() for name in lan.FILES}, before)
                self.assertEqual((leftover / "marker").read_text(), "preserve")
                if attempt == 0:
                    process.kill()  # No finally/context cleanup can run in this child.
                else:
                    process.terminate()
                process.wait(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                process.stdout.close()

    def test_review_rejects_unknown_data_and_changed_credentials(self):
        root, _ = self.prepare_fixture()
        path = root / "unknown"
        private_write(path, "do not remove")
        with self.assertRaises(ValueError):
            self.review_fixture()
        self.assertTrue(path.exists())
        path.unlink()
        for name in ("fixture.json", "provider.json", "tls.key", "tls.crt", "device.token", "devices.json"):
            path = root / name
            path.chmod(0o644)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.review_fixture()
            path.chmod(0o600)
        for name in ("home", "tmp"):
            (root / name).chmod(0o755)
            with self.assertRaises(ValueError):
                self.review_fixture()
            (root / name).chmod(0o700)
        (root / "tls.key").rename(root / "saved-key")
        (root / "tls.key").symlink_to(root / "saved-key")
        with self.assertRaises(ValueError):
            self.review_fixture()
        (root / "tls.key").unlink()
        (root / "saved-key").rename(root / "tls.key")
        key = root / "tls.key"
        saved_key = key.read_bytes()
        key.write_text("not a TLS private key")
        with self.assertRaises((ValueError, OSError)):
            self.review_fixture()
        key.write_bytes(saved_key)
        token = root / "device.token"
        original = token.read_bytes()
        token.write_text("A" * 43 + "\n")
        with self.assertRaises(ValueError):
            self.review_fixture()
        token.write_bytes(original)
        certificate = root / "tls.crt"
        certificate.write_bytes(certificate.read_bytes() + b"\n")
        with self.assertRaises(ValueError):
            self.review_fixture()

    def test_fixed_port_fails_occupied_without_fallback(self):
        with lan.available_socket("127.0.0.1", 0) as first:
            port = first.getsockname()[1]
            with self.assertRaises(OSError):
                lan.available_socket("127.0.0.1", port)
        for port in (-1, 1, 1023, 65536, True):
            with self.subTest(port=port), self.assertRaises(ValueError):
                lan.available_socket("127.0.0.1", port)


class LocalTLSHealth(FixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.make_home()
        self.root, self.manifest = self.prepare_fixture()

    async def test_actual_tls_authenticated_health_without_provider(self):
        from aiohttp import web
        from server.cartesia_agent import CartesiaAgent
        from server.server import Settings
        from server.streaming import create_stream_app
        backend = CartesiaAgent(METADATA["origin"], self.key, METADATA["agent_id"], METADATA["api_version"],
                                agent_version_id=METADATA["agent_version_id"])
        app = create_stream_app(Settings(self.root / "devices.json"), backend)
        context = lan.tls_context(self.root, self.manifest)
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        listener = lan.available_socket("127.0.0.1", 0)
        listener.setblocking(False)
        await runner.setup()
        site = web.SockSite(runner, listener, ssl_context=context)
        try:
            await site.start()
            port = listener.getsockname()[1]
            trusted = ssl.create_default_context(cafile=str(self.root / "tls.crt"))
            token = (self.root / "device.token").read_text().strip()

            async def request(path, bearer=None, client_context=trusted, hostname=HOST):
                reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client_context,
                                                               server_hostname=hostname)
                try:
                    headers = f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n"
                    if bearer:
                        headers += "Authorization: Bearer " + bearer + "\r\n"
                    writer.write((headers + "\r\n").encode("ascii"))
                    await writer.drain()
                    return await asyncio.wait_for(reader.read(16384), 3)
                finally:
                    writer.close()
                    await writer.wait_closed()

            with patch.object(backend, "connect", side_effect=AssertionError("Health must not contact provider")):
                unauthorized = await request("/health")
                self.assertIn(b" 401 ", unauthorized.split(b"\r\n", 1)[0])
                response = await request("/health", token)
                self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
                payload = json.loads(response.split(b"\r\n\r\n", 1)[1])
                self.assertEqual(payload, {"status": "ok", "speech": "cartesia-managed-agent",
                                           "reply": "cartesia-managed-llm"})
                invalid_stream = await request("/voice", token)
                self.assertIn(b" 400 ", invalid_stream.split(b"\r\n", 1)[0])
            with self.assertRaises(ssl.SSLCertVerificationError):
                await request("/health", token, hostname="wrong.example.invalid")
            with self.assertRaises(ssl.SSLCertVerificationError):
                await request("/health", token, client_context=ssl.create_default_context())
        finally:
            await runner.cleanup()
            listener.close()
