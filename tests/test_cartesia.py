"""Real HTTPS/WSS consumer proof for the live transport, never paid providers."""
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

from server.cartesia_agent import CartesiaAgent
from server.server import Settings
from server.streaming import ACTIVE, STATE, create_stream_app
from server.turns import configured_backend
from tools.agent_fixture import AGENT_ID, VERSION_ID, REPLY, TRANSCRIPT, start_agent
from tools.credentials import private_replace
from tools.stream_client import PCMConsumer

ROOT = Path(__file__).resolve().parent.parent
INPUT = b"\x00\x01" * 1600


class StreamingVoiceTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleanup = ExitStack()
        cls.addClassCleanup(cls.cleanup.close)
        cls.home = Path(cls.cleanup.enter_context(tempfile.TemporaryDirectory(prefix="glasses-stream-contract-")))
        cls.secrets = cls.home / "tls"
        subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(cls.secrets), "local-tls"],
                       cwd=ROOT, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=35,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cls.home),
                            "TMPDIR": str(cls.home), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        cls.api = start_agent(cls.home, cls.secrets, cls.cleanup)
        cls.ca = str(cls.secrets / "localhost.crt")

    async def asyncSetUp(self):
        self.api.mode, self.api.transcript, self.api.reply = "ok", TRANSCRIPT, REPLY
        self.api.calls.clear()
        self.api.handshakes.clear()
        self.api.redirect_origin = None
        self.registry = self.home / "devices.json"
        self.devices = {name: hashlib.sha256((token * 43).encode()).hexdigest()
                        for name, token in (("one", "a"), ("two", "b"))}
        private_replace(self.registry, json.dumps(self.devices))
        self.settings = Settings(devices_file=self.registry, requests_per_minute=100)
        self.values = {"VOICE_BACKEND": "cartesia-stream", "CARTESIA_BASE_URL": self.api.origin,
                       "CARTESIA_KEY_FILE": self.api.key_file, "CARTESIA_AGENT_ID": AGENT_ID,
                       "CARTESIA_AGENT_VERSION_ID": VERSION_ID, "CARTESIA_VERSION": "2026-08-14",
                       "CARTESIA_CA_FILE": self.ca, "WEB_SEARCH_BACKEND": "disabled"}
        self.context = ssl.create_default_context(cafile=self.ca)
        self.client = aiohttp.ClientSession(trust_env=False, connector=aiohttp.TCPConnector(ssl=self.context),
                                           timeout=aiohttp.ClientTimeout(total=5))
        self.sockets = []
        await self.start()

    def backend(self, **changes):
        with patch.dict(os.environ, {**self.values, **changes}, clear=True):
            return configured_backend()

    async def start(self, settings=None, backend=None):
        self.app = create_stream_app(settings or self.settings, backend or self.backend())
        self.runner = web.AppRunner(self.app, access_log=None, handler_cancellation=True, shutdown_timeout=1)
        await self.runner.setup()
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(self.ca, self.secrets / "localhost.key")
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=tls)
        await self.site.start()
        self.url = f"https://localhost:{self.site._server.sockets[0].getsockname()[1]}"

    async def restart(self, settings=None, backend=None):
        await self.runner.cleanup()
        await self.start(settings, backend)

    async def asyncTearDown(self):
        for socket in self.sockets:
            await socket.close()
        await self.runner.cleanup()
        await self.client.close()
        await self.no_io()

    def headers(self, token="a", session="stream-test"):
        return {"Authorization": "Bearer " + token * 43, "X-Session": session}

    async def connect(self, token="a", session="stream-test", protocols=("glasses.v1",), headers=None):
        socket = await self.client.ws_connect(self.url + "/voice", headers=self.headers(token, session) if headers is None else headers,
                                              protocols=protocols, compress=0, max_msg_size=3201,
                                              timeout=aiohttp.ClientWSTimeout(ws_close=0.2))
        self.sockets.append(socket)
        return socket

    async def receive(self, socket, consumer):
        async with asyncio.timeout(3):
            return consumer.receive(await socket.receive())

    async def until(self, socket, consumer, target):
        for _ in range(1024):
            kind = await self.receive(socket, consumer)
            if kind == target:
                return
            if kind == "error":
                self.fail("Unexpected stream error: " + consumer.error)
        self.fail("Consumer event bound exceeded")

    async def cancelled(self, socket):
        # A cancelling consumer discards all old PCM until the wire barrier.
        async with asyncio.timeout(3):
            for _ in range(1024):
                message = await socket.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    if message.data == "cancelled":
                        return
                    self.assertFalse(message.data.startswith("error "), message.data)
                else:
                    self.assertEqual(message.type, aiohttp.WSMsgType.BINARY)
        self.fail("Missing cancellation acknowledgement")

    async def no_io(self):
        async with asyncio.timeout(2):
            while self.app[ACTIVE] or self.api.active:
                await asyncio.sleep(0.01)
        leaked = [task.get_name() for task in asyncio.all_tasks()
                  if task is not asyncio.current_task() and task.get_name().startswith("glasses-")
                  and task.get_name() != "glasses-session-reaper" and not task.done()]
        self.assertEqual(leaked, [])
        self.assertTrue(all(call["closed"].is_set() for call in self.api.calls))

    async def final(self, token="a", session="stream-test", expected=None):
        socket, consumer = await self.connect(token, session), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "done")
        if expected is not None:
            self.assertEqual(bytes(consumer.pcm), expected)
        with self.assertRaises(ValueError):
            consumer.played()
        while consumer.pcm:
            consumer.drain()
        await socket.send_str(consumer.played())
        await self.until(socket, consumer, "complete")
        await socket.close()
        await self.no_io()
        return consumer

    async def test_completed_early_answer_is_delivered_live_then_cleared_by_later_input(self):
        self.api.mode = "completed-during-input"
        socket, consumer = await self.connect(), PCMConsumer()
        self.assertEqual(socket.protocol, "glasses.v1")
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "done")
        # This assertion is consumer-visible: old PCM has arrived BEFORE the
        # producer even sends later input. Buffered replay adapters cannot pass.
        self.assertEqual(bytes(consumer.pcm), b"\x01\x00" * 8000)
        self.assertEqual(self.api.calls[-1]["input_bytes"], 3200)
        self.assertEqual(list(self.app[STATE].sessions[("one", "stream-test")].messages), [])
        old_revision = consumer.revision
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "clear")
        self.assertEqual(consumer.revision, old_revision)
        self.assertEqual(consumer.pcm, b"")
        self.assertFalse(consumer.done)
        with self.assertRaises(ValueError):
            consumer.played()
        await self.until(socket, consumer, "done")
        self.assertGreater(consumer.revision, old_revision)
        self.assertEqual(bytes(consumer.pcm), b"\x80\x00" * 8000)
        self.assertEqual(self.api.calls[-1]["completed_at_input_bytes"], [3200, 6400])
        self.assertEqual(list(self.app[STATE].sessions[("one", "stream-test")].messages), [])
        while consumer.pcm:
            consumer.drain(640)
        await socket.send_str(consumer.played())
        await self.until(socket, consumer, "complete")
        self.assertEqual(list(self.app[STATE].sessions[("one", "stream-test")].messages), [
            {"role": "user", "content": TRANSCRIPT + " Please be concise."},
            {"role": "assistant", "content": REPLY}])
        await socket.close()
        await self.no_io()

    async def test_unicode_final_only_and_device_history_isolation(self):
        self.api.transcript, self.api.reply = "Question about café?", "Café.\nFinal answer."
        await self.final(expected=b"\x80\x00" * 8000)
        await self.final(token="b")
        await self.final()
        pair = [{"role": "user", "content": self.api.transcript}, {"role": "assistant", "content": self.api.reply}]
        self.assertEqual([record["history"] for record in self.api.calls], [[], [], pair])
        self.assertEqual(list(self.app[STATE].sessions[("one", "stream-test")].messages), pair * 2)

    async def test_pcm_minimum_and_maximum_are_sample_exact(self):
        for mode, samples in (("minimum", 320), ("maximum", 480000)):
            with self.subTest(mode=mode):
                self.api.mode = mode
                await self.final(session=mode, expected=b"\x80\x00" * samples)

    async def test_invalid_partial_action_and_excess_provider_output_never_commit(self):
        for index, mode in enumerate(("partial", "odd-audio", "short-audio", "oversized-audio", "error", "unknown-tool",
                                      "binary", "malformed-json", "interrupted-user", "empty-transcript",
                                      "oversized-message", "event-flood")):
            with self.subTest(mode=mode):
                self.api.mode = mode
                if mode == "partial":
                    await self.restart(replace(self.settings, request_timeout=0.35))
                socket, consumer = await self.connect(session=f"fault-{index}"), PCMConsumer()
                await self.until(socket, consumer, "ready")
                await socket.send_bytes(INPUT)
                await self.until(socket, consumer, "error")
                self.assertIn(consumer.error, ("upstream", "timeout"))
                self.assertEqual(consumer.pcm, b"")
                await socket.close()
                await self.no_io()
                self.assertEqual(self.app[STATE].sessions, {})
                if mode == "partial":
                    await self.restart()

    async def test_version_and_format_are_verified_before_ready_or_any_input(self):
        for mode in ("version-drift", "wrong-format", "before-ready"):
            with self.subTest(mode=mode):
                self.api.mode = mode
                socket, consumer = await self.connect(), PCMConsumer()
                await self.until(socket, consumer, "error")
                self.assertFalse(consumer.ready)
                self.assertEqual(consumer.error, "upstream")
                await socket.close()
                await self.no_io()
                self.assertEqual(self.api.calls[-1]["input_bytes"], 0)
                self.assertEqual(self.app[STATE].sessions, {})

    async def test_redirect_cannot_forward_custom_key_even_to_another_tls_origin(self):
        target_home = self.home / "redirect-target"
        target_home.mkdir()
        with ExitStack() as cleanup:
            target = start_agent(target_home, self.secrets, cleanup)
            self.api.mode, self.api.redirect_origin = "redirect", target.origin
            socket, consumer = await self.connect(), PCMConsumer()
            await self.until(socket, consumer, "error")
            self.assertFalse(consumer.ready)
            await socket.close()
            await self.no_io()
            self.assertEqual(len(self.api.handshakes), 1)
            self.assertEqual(target.handshakes, [])
            self.assertEqual(self.api.calls, [])

    async def test_provider_tls_hostname_and_ca_failure_expose_no_audio(self):
        for changes in ({"CARTESIA_CA_FILE": ""}, {"CARTESIA_BASE_URL": self.api.origin.replace("localhost", "127.0.0.1")}):
            with self.subTest(changes=changes):
                await self.restart(backend=self.backend(**changes))
                socket, consumer = await self.connect(), PCMConsumer()
                await self.until(socket, consumer, "error")
                self.assertFalse(consumer.ready)
                await socket.close()
                await self.no_io()
        self.assertEqual(self.api.calls, [])

    async def test_health_and_removed_finite_route_never_start_provider_calls(self):
        async with self.client.get(self.url + "/health", headers=self.headers()) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"status": "ok", "speech": "cartesia-managed-agent", "reply": "cartesia-managed-llm"})
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        async with self.client.post(self.url + "/ask", headers=self.headers(), data=b"not a managed WAV route") as response:
            self.assertEqual(response.status, 404)
        self.assertEqual(self.api.handshakes, [])

    async def test_auth_session_subprotocol_and_origin_rejected_before_upgrade(self):
        cases = (({}, ("glasses.v1",), 401), (self.headers("z"), ("glasses.v1",), 401),
                 ({"Authorization": "Bearer " + "a" * 43}, ("glasses.v1",), 400),
                 (self.headers(session="bad/session"), ("glasses.v1",), 400),
                 (self.headers(), (), 400), (self.headers(), ("other.v1",), 400),
                 ({**self.headers(), "Origin": "https://untrusted.invalid"}, ("glasses.v1",), 400))
        for headers, protocols, status in cases:
            with self.subTest(headers=list(headers), protocols=protocols):
                with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                    await self.connect(headers=headers, protocols=protocols)
                self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.api.handshakes, [])

    async def test_rate_busy_and_session_limits_are_preserved(self):
        self.api.mode = "idle"
        await self.restart(replace(self.settings, max_concurrent=2, sessions_per_device=1))
        first, c1 = await self.connect(), PCMConsumer()
        await self.until(first, c1, "ready")
        for token, session, status in (("a", "stream-test", 409), ("a", "other", 429)):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
                await self.connect(token, session)
            self.assertEqual(caught.exception.status, status)
        second, c2 = await self.connect("b"), PCMConsumer()
        await self.until(second, c2, "ready")
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.connect("b", "third")
        self.assertEqual(caught.exception.status, 503)
        await first.close()
        await second.close()
        await self.no_io()
        await self.restart(replace(self.settings, requests_per_minute=1))
        first, c1 = await self.connect(), PCMConsumer()
        await self.until(first, c1, "ready")
        await first.close()
        await self.no_io()
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.connect()
        self.assertEqual(caught.exception.status, 429)

    async def test_odd_empty_oversized_input_controls_and_silence_flush_without_history(self):
        self.api.mode = "idle"
        for index, value in enumerate((b"", b"x", b"x" * 3202, "unknown", "played 0", "played 2147483648", "x" * 65)):
            with self.subTest(value_type=type(value).__name__, length=len(value)):
                socket, consumer = await self.connect(session=f"input-{index}"), PCMConsumer()
                await self.until(socket, consumer, "ready")
                await (socket.send_bytes(value) if isinstance(value, bytes) else socket.send_str(value))
                await self.until(socket, consumer, "error")
                self.assertIn(consumer.error, ("invalid_audio", "protocol"))
                await socket.close()
                await self.no_io()
                self.assertEqual(self.app[STATE].sessions, {})
        socket, consumer = await self.connect(), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(b"\0\0" * 320)
        await socket.send_str("stop_input")
        await self.until(socket, consumer, "error")
        self.assertEqual(consumer.error, "silence")
        await socket.close()
        await self.no_io()
        self.assertEqual(self.app[STATE].sessions, {})

    async def test_total_input_cap_and_stop_are_enforced_without_provider_commit_message(self):
        self.api.mode = "idle"
        for stopped in (False, True):
            socket, consumer = await self.connect(), PCMConsumer()
            await self.until(socket, consumer, "ready")
            if stopped:
                await socket.send_bytes(INPUT)
                await socket.send_str("stop_input")
                await socket.send_bytes(INPUT)
            else:
                for _ in range(101):
                    await socket.send_bytes(INPUT)
            await self.until(socket, consumer, "error")
            self.assertEqual(consumer.error, "invalid_audio")
            await socket.close()
            await self.no_io()
            self.assertEqual(self.app[STATE].sessions, {})

    async def test_wrong_or_cleared_acknowledgement_cannot_commit(self):
        self.api.mode = "completed-during-input"
        socket, consumer = await self.connect(), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "done")
        old = consumer.revision
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "clear")
        await socket.send_str(f"played {old}")
        await self.until(socket, consumer, "error")
        self.assertEqual(consumer.error, "protocol")
        self.assertEqual(consumer.pcm, b"")
        await socket.close()
        await self.no_io()
        self.assertEqual(self.app[STATE].sessions, {})

    async def test_revocation_cancels_stalled_upstream_without_tasks_or_io(self):
        self.api.mode = "timeout"
        socket, consumer = await self.connect(), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        self.devices.pop("one")
        private_replace(self.registry, json.dumps(self.devices))
        start = time.monotonic()
        await self.until(socket, consumer, "error")
        self.assertEqual(consumer.error, "unauthorized")
        await socket.close()
        await self.no_io()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.app[STATE].sessions, {})

    async def test_deadline_cancels_real_stalled_provider_and_releases_slot(self):
        self.api.mode = "timeout"
        await self.restart(replace(self.settings, request_timeout=0.3, max_concurrent=1))
        socket, consumer = await self.connect(), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        start = time.monotonic()
        await self.until(socket, consumer, "error")
        self.assertEqual(consumer.error, "timeout")
        await socket.close()
        await self.no_io()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.app[STATE].sessions, {})
        self.api.mode = "ok"
        await self.final()

    async def test_client_cancellation_before_ready_and_during_output_closes_real_upstream(self):
        for mode in ("hold-ready", "ok"):
            with self.subTest(mode=mode):
                self.api.mode = mode
                socket, consumer = await self.connect(), PCMConsumer()
                if mode == "ok":
                    await self.until(socket, consumer, "ready")
                    await socket.send_bytes(INPUT)
                    await self.until(socket, consumer, "audio")
                else:
                    async with asyncio.timeout(2):
                        while not self.api.calls:
                            await asyncio.sleep(0.01)
                await socket.close()
                await self.no_io()
                self.assertEqual(self.app[STATE].sessions, {})

    async def test_cancelled_waits_for_provider_cleanup_before_immediate_reconnect(self):
        backend = self.backend()
        connect = backend.connect
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def slow_cleanup(history):
            # Keep both real WSS transports and the real provider adapter; hold
            # only cleanup so the close/reconnect interleaving is deterministic.
            async with connect(history) as upstream:
                try:
                    yield upstream
                finally:
                    cleanup_started.set()
                    await release_cleanup.wait()

        backend.connect = slow_cleanup
        await self.restart(replace(self.settings, max_concurrent=1), backend)
        socket, consumer = await self.connect(), PCMConsumer()
        await self.until(socket, consumer, "ready")
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "audio")
        await socket.send_str("cancel")
        acknowledgement = asyncio.create_task(self.cancelled(socket))
        try:
            async with asyncio.timeout(1):
                await cleanup_started.wait()
            done, _ = await asyncio.wait((acknowledgement,), timeout=0.03)
            self.assertFalse(done, "cancelled was emitted before provider cleanup")
            self.assertTrue(self.app[STATE].sessions[("one", "stream-test")].busy)
        finally:
            release_cleanup.set()
            await acknowledgement
        # The wire acknowledgement is the ONLY reconnect barrier: no close
        # handshake, internal cleanup polling or retry before the new WSS.
        await self.final()
        self.assertIn((await socket.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))

        # A cancellation does not bypass the post-cleanup authorization check.
        cleanup_started.clear()
        release_cleanup.clear()
        self.api.mode = "idle"
        revoked, consumer = await self.connect(), PCMConsumer()
        await self.until(revoked, consumer, "ready")
        await revoked.send_str("cancel")
        try:
            async with asyncio.timeout(1):
                await cleanup_started.wait()
            self.devices.pop("one")
            private_replace(self.registry, json.dumps(self.devices))
        finally:
            release_cleanup.set()
        await self.until(revoked, consumer, "error")
        self.assertEqual(consumer.error, "unauthorized")
        self.assertIn((await revoked.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        await self.no_io()
        calls = len(self.api.calls)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as caught:
            await self.connect()
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(len(self.api.calls), calls)

    async def test_cancel_before_ready_allows_immediate_same_session_replacement(self):
        await self.restart(replace(self.settings, max_concurrent=1))
        self.api.mode = "hold-ready"
        old = await self.connect()
        async with asyncio.timeout(2):
            while not self.api.calls:
                await asyncio.sleep(0.01)
        await old.send_str("cancel")
        await self.cancelled(old)
        self.api.mode = "ok"
        await self.final()
        self.assertEqual(self.api.calls[0]["input_bytes"], 0)
        self.assertEqual(self.api.calls[1]["history"], [])
        self.assertIn((await old.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))

    async def test_complete_hands_off_same_session_when_played_precedes_cancel(self):
        await self.restart(replace(self.settings, max_concurrent=1))
        old, consumer = await self.connect(), PCMConsumer()
        await self.until(old, consumer, "ready")
        await old.send_bytes(INPUT)
        await self.until(old, consumer, "done")
        while consumer.pcm:
            consumer.drain()
        await old.send_str(consumer.played())
        # Near-end speech can overlap a played acknowledgement already queued
        # on the wire. Accepted played wins: complete, not cancelled, is the
        # terminal handoff and this successful pair must remain committed.
        await old.send_str("cancel")
        await self.until(old, consumer, "complete")

        # No old socket close, no_io polling or retry before replacement WSS.
        self.api.transcript, self.api.reply = "Next question.", "Next answer."
        await self.final(expected=b"\x80\x00" * 8000)
        seed = [{"role": "user", "content": TRANSCRIPT}, {"role": "assistant", "content": REPLY}]
        self.assertEqual([call["history"] for call in self.api.calls], [[], seed])
        self.assertEqual(list(self.app[STATE].sessions[("one", "stream-test")].messages), seed + [
            {"role": "user", "content": "Next question."},
            {"role": "assistant", "content": "Next answer."}])
        self.assertIn((await old.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))

    async def test_barge_in_reconnect_preserves_committed_history_without_old_audio_or_pair(self):
        await self.restart(replace(self.settings, max_concurrent=1))
        seed = [{"role": "user", "content": TRANSCRIPT}, {"role": "assistant", "content": REPLY}]
        for boundary in ("audio", "done"):
            with self.subTest(boundary=boundary):
                session = "barge-in-" + boundary
                self.api.mode, self.api.transcript, self.api.reply = "ok", TRANSCRIPT, REPLY
                await self.final(session=session)

                self.api.mode, self.api.transcript = "completed-during-input", "Abandoned question."
                old, abandoned = await self.connect(session=session), PCMConsumer()
                await self.until(old, abandoned, "ready")
                await old.send_bytes(INPUT)
                # Transport input can end while physical capture continues.
                # Interruption cancels the exchange, not its transport input.
                await old.send_str("stop_input")
                await self.until(old, abandoned, boundary)
                self.assertTrue(abandoned.pcm)
                self.assertEqual(bytes(abandoned.pcm), b"\x01\x00" * (len(abandoned.pcm) // 2))
                cancelled_call = self.api.calls[-1]
                self.assertEqual(cancelled_call["history"], seed)
                self.assertEqual(list(self.app[STATE].sessions[("one", session)].messages), seed)
                # Near-end VAD drops the old sink locally and NEVER acknowledges
                # its queued PCM, even if the provider has already emitted done.
                abandoned.pcm.clear()
                abandoned.done = False
                buffered_utterance = (INPUT, b"\x00\x02" * 320)
                await old.send_str("cancel")
                await self.cancelled(old)
                self.assertEqual(abandoned.acknowledged, 0)
                self.assertFalse(abandoned.complete)

                # No polling/retry or no_io barrier before the replacement:
                # cancelled must release the sole concurrency slot and same
                # session before this immediate replacement connection.
                self.api.mode = "ok"
                self.api.transcript, self.api.reply = "New buffered question.", "Only the new answer."
                current, replacement = await self.connect(session=session), PCMConsumer()
                await self.until(current, replacement, "ready")
                self.assertIn((await old.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
                for chunk in buffered_utterance:
                    await current.send_bytes(chunk)
                await current.send_str("stop_input")
                await self.until(current, replacement, "done")
                self.assertEqual(bytes(replacement.pcm), b"\x80\x00" * 8000)
                self.assertEqual(self.api.calls[-1]["history"], seed)
                self.assertEqual(list(self.app[STATE].sessions[("one", session)].messages), seed)
                while replacement.pcm:
                    replacement.drain()
                await current.send_str(replacement.played())
                await self.until(current, replacement, "complete")
                await current.close()
                await self.no_io()
                self.assertTrue(cancelled_call["closed"].is_set())
                self.assertEqual(cancelled_call["input_bytes"], len(INPUT))
                self.assertEqual(self.api.calls[-1]["input_bytes"], sum(map(len, buffered_utterance)))
                self.assertEqual(list(self.app[STATE].sessions[("one", session)].messages), seed + [
                    {"role": "user", "content": "New buffered question."},
                    {"role": "assistant", "content": "Only the new answer."}])

    async def test_input_before_provider_ready_is_rejected_and_not_forwarded(self):
        self.api.mode = "hold-ready"
        socket, consumer = await self.connect(), PCMConsumer()
        await socket.send_bytes(INPUT)
        await self.until(socket, consumer, "error")
        self.assertFalse(consumer.ready)
        self.assertEqual(consumer.error, "protocol")
        await socket.close()
        await self.no_io()
        self.assertTrue(all(call["input_bytes"] == 0 for call in self.api.calls))

    async def test_missing_and_conflicting_configuration_never_falls_back(self):
        for name in ("CARTESIA_AGENT_ID", "CARTESIA_AGENT_VERSION_ID", "CARTESIA_KEY_FILE", "CARTESIA_VERSION"):
            values = {key: value for key, value in self.values.items() if key != name}
            with self.subTest(missing=name), patch.dict(os.environ, values, clear=True), self.assertRaises(KeyError):
                configured_backend()
        for changes in ({"VOICE_BACKEND": "cartesia-agent"}, {"VOICE_BACKEND": "unknown"},
                        {"SPEECH_BACKEND": "mock"}, {"REPLY_BACKEND": "direct"}, {"HERMES_BACKEND": "http"},
                        {"WEB_SEARCH_BACKEND": "deepseek"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.backend(**changes)
        self.assertIsInstance(self.backend(), CartesiaAgent)
        self.assertEqual(self.api.handshakes, [])


class ConsumerCancellationTests(unittest.TestCase):
    def test_cancel_discards_inflight_audio_and_cannot_acknowledge_old_playback(self):
        consumer = PCMConsumer()
        text = lambda value: aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, value, "")
        consumer.receive(text("ready"))
        consumer.receive(text("speaking 1"))
        pcm = aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, b"\x01\x00" * 320, "")
        consumer.receive(pcm)
        self.assertEqual(consumer.cancel(), "cancel")
        self.assertEqual(consumer.drain(), b"")
        consumer.receive(pcm)
        self.assertEqual(consumer.drain(), b"")
        consumer.receive(text("done 1"))
        with self.assertRaises(ValueError):
            consumer.played()
        consumer.receive(text("cancelled"))
        self.assertTrue(consumer.cancelled)
        self.assertEqual(consumer.acknowledged, 0)
        with self.assertRaises(ValueError):
            consumer.receive(pcm)

    def test_unsolicited_cancel_acknowledgement_is_rejected(self):
        consumer = PCMConsumer()
        with self.assertRaises(ValueError):
            consumer.receive(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, "cancelled", ""))
        self.assertFalse(consumer.cancelled)


if __name__ == "__main__":
    unittest.main()
