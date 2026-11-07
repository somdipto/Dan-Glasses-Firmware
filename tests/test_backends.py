"""Private upstream protocol and real wall-clock failure bounds; no live services."""
import json
import os
import math
import shutil
import struct
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server.audio import make_wav, parse_wav
from server.backends import ChatHTTP, UpstreamError, UpstreamTimeout, configured_backends, piper_reply_wav, validate_chat_url


class FakeChat(BaseHTTPRequestHandler):
    mode = "ok"
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).calls.append((self.path, dict(self.headers), body))
        mode = type(self).mode
        if mode == "slow":
            time.sleep(1)
        data = {"choices": [{"message": {"role": "assistant", "content": "你好，café."}, "finish_reason": "stop"}]}
        if mode == "partial":
            data["choices"][0]["finish_reason"] = "length"
        if mode == "tool-call":
            data["choices"][0]["finish_reason"] = "tool_calls"
        if mode == "non-text":
            data["choices"][0]["message"]["content"] = [{"type": "text", "text": "not a final string"}]
        if mode == "long-text":
            data["choices"][0]["message"]["content"] = "a" * 2049
        raw = json.dumps(data, ensure_ascii=False).encode()
        if mode == "invalid-json":
            raw = b"private upstream failure details, not JSON"
        if mode == "oversized":
            raw = b"x" * 70000
        self.send_response(503 if mode == "failure" else 307 if mode == "redirect" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if mode == "redirect":
            self.send_header("Location", "/not-allowed")
        if mode == "encoded":
            self.send_header("Content-Encoding", "gzip")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), FakeChat)
        cls.http.daemon_threads = True
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(2)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = Path(self.tmp.name) / "llm.key"
        self.key.write_text("fixture-not-real\n")
        self.key.chmod(0o600)
        self.adapter = ChatHTTP(f"http://127.0.0.1:{self.http.server_port}/v1", self.key, "fixture-model", api_style="openai")
        FakeChat.calls, FakeChat.mode = [], "ok"


    def test_direct_api_is_stateless_bounded_and_has_no_hermes_headers(self):
        adapter = ChatHTTP(f"http://127.0.0.1:{self.http.server_port}/v1", self.key, "fixture-model", api_style="openai")
        previous = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "reply"}]
        self.assertEqual(adapter.chat(previous, "你好", "scope-a", 5), "你好，café.")
        path, headers, body = FakeChat.calls[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer fixture-not-real")
        self.assertFalse(any(name.lower().startswith("x-hermes-") for name in headers))
        self.assertEqual(body["model"], "fixture-model")
        self.assertEqual(body["messages"][1:], previous + [{"role": "user", "content": "你好"}])
        self.assertEqual(body["max_tokens"], 256)
        self.assertFalse(body["stream"])
        self.assertNotIn("store", body)
        self.assertNotIn("tools", body)

    def test_explicit_direct_configuration_never_falls_back_to_mock(self):
        values = {"REPLY_BACKEND": "direct", "SPEECH_BACKEND": "mock",
                  "LLM_BASE_URL": f"http://127.0.0.1:{self.http.server_port}/v1",
                  "LLM_MODEL": "fixture-model", "LLM_KEY_FILE": str(self.key), "LLM_API_STYLE": "openai"}
        for missing in ("LLM_BASE_URL", "LLM_MODEL", "LLM_KEY_FILE", "LLM_API_STYLE"):
            with self.subTest(missing=missing), patch.dict(os.environ, {k: v for k, v in values.items() if k != missing}, clear=True):
                with self.assertRaises(KeyError):
                    configured_backends()
        for invalid in ({"REPLY_BACKEND": "invalid"}, {"REPLY_BACKEND": "hermes"}, {"HERMES_BACKEND": "http"}):
            with self.subTest(config=invalid), patch.dict(os.environ, invalid, clear=True):
                with self.assertRaises(ValueError):
                    configured_backends()

    def test_upstream_errors_partial_non_text_and_size_limits_fail_closed(self):
        for mode in ("partial", "tool-call", "non-text", "long-text", "invalid-json", "oversized", "encoded", "failure", "redirect"):
            with self.subTest(mode=mode):
                FakeChat.mode = mode
                before = len(FakeChat.calls)
                with self.assertRaises(UpstreamError) as caught:
                    self.adapter.chat([], "input", "scope", 5)
                self.assertEqual(str(caught.exception), "")
                self.assertEqual(len(FakeChat.calls), before + 1)  # No retry or redirect.

    def test_wall_clock_timeout_terminates_worker_and_capacity_recovers(self):
        FakeChat.mode = "slow"
        start = time.monotonic()
        with self.assertRaises(UpstreamTimeout):
            self.adapter.chat([], "input", "scope", 0.3)
        self.assertLess(time.monotonic() - start, 1.5)
        FakeChat.mode = "ok"
        self.assertEqual(self.adapter.chat([], "input", "scope", 5), "你好，café.")

    def test_worker_exit_at_timeout_keeps_timeout_result(self):
        FakeChat.mode = "slow"
        killpg = os.killpg

        def exited_group(pid, sig):
            killpg(pid, sig)  # Never leave the owned child running when simulating ESRCH.
            raise ProcessLookupError()

        with patch("server.backends.os.killpg", side_effect=exited_group):
            with self.assertRaises(UpstreamTimeout):
                self.adapter.chat([], "input", "scope", 0.3)
        FakeChat.mode = "ok"
        self.assertEqual(self.adapter.chat([], "input", "scope", 5), "你好，café.")

    def test_no_insecure_remote_url_or_credential_in_url(self):
        for url in ("http://example.com/v1", "http://localhost/v1", "http://10.0.0.1/v1",
                    "https://user:password@example.com/v1", "https://example.com/v1?key=bad", "https://example.com"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_chat_url(url)
        self.assertEqual(validate_chat_url("https://internal.example/v1"), "https://internal.example/v1")

    def test_missing_or_publicly_readable_upstream_secret_is_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            ChatHTTP("http://127.0.0.1:8642/v1", self.key, "fixture-model", api_style="openai")
        self.key.unlink()
        with self.assertRaises(OSError):
            ChatHTTP("http://127.0.0.1:8642/v1", self.key, "fixture-model", api_style="openai")

    def test_chat_tls_requires_trusted_ca_and_matching_hostname(self):
        root = Path(__file__).resolve().parent.parent
        directory = Path(self.tmp.name) / "tls"
        subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(directory), "local-tls"],
                       cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=35)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / "localhost.crt", directory / "localhost.key")
        http = ThreadingHTTPServer(("127.0.0.1", 0), FakeChat)
        http.daemon_threads = True
        http.socket = context.wrap_socket(http.socket, server_side=True)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            for host, ca in (("localhost", None), ("127.0.0.1", directory / "localhost.crt")):
                adapter = ChatHTTP(f"https://{host}:{http.server_port}/v1", self.key, "fixture-model", ca_file=ca, api_style="openai")
                with self.assertRaises(UpstreamError):
                    adapter.chat([], "fixture", "scope", 5)
            self.assertEqual(FakeChat.calls, [])
            adapter = ChatHTTP(f"https://localhost:{http.server_port}/v1", self.key, "fixture-model",
                               ca_file=directory / "localhost.crt", api_style="openai")
            self.assertEqual(adapter.chat([], "fixture", "scope", 5), "你好，café.")
        finally:
            http.shutdown()
            http.server_close()
            thread.join(2)

    def test_secret_entry_refuses_piped_input_without_creating_a_key(self):
        root = Path(__file__).resolve().parent.parent
        directory = Path(self.tmp.name) / "credentials"
        secret = "fixture-value-that-must-not-be-printed"
        result = subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(directory), "store", "cartesia"],
                                cwd=root, input=secret + "\n", capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((directory / "cartesia.key").exists())
        self.assertNotIn(secret, result.stdout + result.stderr)


class AudioFormatTests(unittest.TestCase):
    def test_input_and_output_duration_sample_boundaries(self):
        for output, minimum, maximum in ((False, 4800, 160000), (True, 320, 480000)):
            for frames in (minimum, maximum):
                with self.subTest(output=output, frames=frames):
                    wav = make_wav(1, 16000, 16, b"\x80\0" * frames)
                    self.assertEqual(parse_wav(wav, output=output), (1, 16000, 16, b"\x80\0" * frames))
            for frames in (minimum - 1, maximum + 1):
                with self.subTest(output=output, frames=frames), self.assertRaises(ValueError):
                    parse_wav(make_wav(1, 16000, 16, b"\0\0" * frames), output=output)

    def test_metadata_is_bounded_without_weakening_pcm_or_length_validation(self):
        wav = make_wav(1, 16000, 16, b"\x80\0" * 8000)
        # Odd-size ancillary chunks require padding; a fmt extension must be empty.
        metadata = b"JUNK" + struct.pack("<I", 3) + b"abc\0"
        extended_fmt = wav[12:16] + struct.pack("<I", 18) + wav[20:36] + b"\0\0"
        body = extended_fmt + metadata + wav[36:]
        with_metadata = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body
        self.assertEqual(parse_wav(with_metadata, output=True), parse_wav(wav, output=True))
        invalid = (
            with_metadata[:-1],
            with_metadata + b"trailer",
            wav[:4] + b"\xff" * 4 + wav[8:],
            wav[:40] + b"\xff" * 4 + wav[44:],
            wav[:28] + struct.pack("<I", 48000) + wav[32:],
            with_metadata[:36] + b"\x01\0" + with_metadata[38:],
        )
        for data in invalid:
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                parse_wav(data, output=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "Optional local speech requires FFmpeg")
class LocalResamplingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="glasses-local-resample-")
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / "piper.wav"

    def convert(self, rate, pcm):
        self.source.write_bytes(make_wav(1, rate, 16, pcm))
        return piper_reply_wav(self.source)

    def test_native_16k_pcm_is_preserved_exactly(self):
        pcm = struct.pack("<6h", -32768, -20000, -1, 0, 20000, 32767) * 100
        self.assertEqual(self.convert(16000, pcm), make_wav(1, 16000, 16, pcm))

    def test_real_resampler_preserves_inclusive_duration_boundaries(self):
        for rate in (22050, 24000):
            for frames, expected in ((rate // 50, 320), (rate * 30, 480000)):
                with self.subTest(rate=rate, frames=frames):
                    wav = self.convert(rate, b"\x80\0" * frames)
                    channels, board_rate, bits, pcm = parse_wav(wav, output=True)
                    self.assertEqual((channels, board_rate, bits), (1, 16000, 16))
                    self.assertLessEqual(abs(len(pcm) // 2 - expected), 1)
                    self.assertEqual(len(wav), 44 + len(pcm))
            for frames in (rate // 50 - 1, rate * 30 + 1):
                with self.subTest(rate=rate, frames=frames), self.assertRaises(UpstreamError):
                    self.convert(rate, b"\0\0" * frames)

    def test_downsampling_filters_above_board_nyquist_instead_of_aliasing(self):
        for rate in (22050, 24000):
            with self.subTest(rate=rate):
                frames = rate // 2
                source = struct.pack(f"<{frames}h", *(int(10000 * math.sin(2 * math.pi * 9000 * i / rate))
                                                    for i in range(frames)))
                _, board_rate, _, pcm = parse_wav(self.convert(rate, source), output=True)
                samples = [value[0] for value in struct.iter_unpack("<h", pcm)]
                interior = samples[board_rate // 10:-board_rate // 10]
                rms = math.sqrt(sum(value * value for value in interior) / len(interior))
                self.assertLess(rms, 100)  # A relabel/linear shim aliases a loud 7 kHz tone.


if __name__ == "__main__":
    unittest.main()
