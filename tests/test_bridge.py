"""Security and consumer behavior. Run python -m unittest discover -s tests."""
import hashlib
import json
import math
import shutil
import struct
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from server.server import Settings, create_app
from server.audio import make_wav, parse_wav
from server.backends import UpstreamError, UpstreamTimeout, piper_reply_wav
from server.turns import SpeechReplyTurn

TOKENS = {"one": "a" * 43, "two": "b" * 43}


def audio(seconds=0.5, silent=False, rate=16000):
    samples = int(rate * seconds)
    pcm = b"\0\0" * samples if silent else struct.pack(
        f"<{samples}h", *(int(4000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(samples)))
    return make_wav(1, rate, 16, pcm)


class Speech:
    def __init__(self):
        self.heard = "你好，café?"
        self.spoken = []
        self.failure = None
        self.calls = 0

    def transcribe(self, wav, timeout):
        self.calls += 1
        if self.failure:
            raise self.failure
        return self.heard

    def synthesize(self, text, timeout):
        self.spoken.append(text)
        return audio()


class Reply:
    def __init__(self):
        self.calls = []
        self.reply = "你好，café."
        self.failure = None
        self.started = None
        self.release = None

    def chat(self, history, text, scope, timeout):
        self.calls.append((list(history), text, scope))
        if self.started:
            self.started.set()
            self.release.wait(3)
        if self.failure:
            raise self.failure
        return self.reply


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = Path(self.tmp.name) / "devices.json"
        self.write_registry(TOKENS)
        self.settings = Settings(devices_file=self.registry)
        self.speech, self.reply = Speech(), Reply()
        self.app = create_app(self.settings, SpeechReplyTurn(self.speech, self.reply))
        self.client = self.app.test_client()
        self.wav = audio()

    def write_registry(self, tokens):
        self.registry.write_text(json.dumps({k: hashlib.sha256(v.encode()).hexdigest() for k, v in tokens.items()}))
        self.registry.chmod(0o600)

    def headers(self, device="one", session="session-a"):
        return {"Authorization": f"Bearer {TOKENS[device]}", "X-Session": session,
                "Content-Type": "audio/wav"}

    def ask(self, device="one", session="session-a", data=None, client=None):
        body = self.wav if data is None else data
        return (client or self.client).post("/ask", data=body, headers=self.headers(device, session),
                                          environ_overrides={"CONTENT_LENGTH": str(len(body))})

    def test_missing_wrong_and_unicode_credentials_are_rejected_on_every_route(self):
        for authorization in (None, "Bearer wrong", "Bearer " + "c" * 43, "Bearer café"):
            for route, method in (("/ask", "post"), ("/health", "get"), ("/reset", "post")):
                with self.subTest(auth=authorization, route=route):
                    headers = {} if authorization is None else {"Authorization": authorization}
                    result = getattr(self.client, method)(route, headers=headers)
                    self.assertEqual(result.status_code, 401)
        self.assertEqual(self.reply.calls, [])

    def test_health_does_not_call_upstreams_or_disclose_configuration(self):
        r = self.client.get("/health", headers=self.headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["status"], "ok")
        self.assertEqual(self.speech.calls, 0)
        self.assertNotIn(str(self.registry), r.text)

    def test_malformed_audio_and_format_boundaries(self):
        malformed = [b"", b"RIFF", self.wav[:-1], self.wav + b"trailer",
                     self.wav[:16] + struct.pack("<I", 0xFFFFFFFF) + self.wav[20:],
                     self.wav[:32] + b"\0\0" + self.wav[34:], audio(rate=8000),
                     audio(seconds=0.1), audio(seconds=10.001)]
        for data in malformed:
            with self.subTest(length=len(data)):
                app = create_app(self.settings, SpeechReplyTurn(self.speech, self.reply))
                self.assertEqual(self.ask(data=data, client=app.test_client()).status_code, 422)
        self.assertEqual(self.reply.calls, [])

    def test_oversize_and_wrong_media_type(self):
        self.assertEqual(self.ask(data=b"x" * (self.settings.max_upload_bytes + 1)).status_code, 413)
        self.assertEqual(self.client.post("/ask", data=self.wav, headers={
            **self.headers(), "Content-Type": "application/octet-stream"}).status_code, 415)

    def test_missing_length_and_bad_session(self):
        self.assertEqual(self.client.post("/ask", headers=self.headers(),
                                         environ_overrides={"CONTENT_LENGTH": ""}).status_code, 411)
        for session in ("", "../x", "x" * 65, "café"):
            self.assertEqual(self.ask(session=session).status_code, 400)

    def test_silence_and_empty_transcription_do_not_invoke_reply(self):
        self.assertEqual(self.ask(data=audio(silent=True)).status_code, 204)
        self.assertEqual(self.speech.calls, 0)
        self.speech.heard = "   "
        self.assertEqual(self.ask().status_code, 204)
        self.assertEqual(self.reply.calls, [])

    def test_recognition_already_leases_the_session_and_reset(self):
        started, release = threading.Event(), threading.Event()

        def transcribe(wav, timeout):
            self.speech.calls += 1
            started.set()
            release.wait(3)
            return self.speech.heard

        self.speech.transcribe = transcribe
        results = []
        thread = threading.Thread(target=lambda: results.append(self.ask(client=self.app.test_client()).status_code))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.ask(client=self.app.test_client()).status_code, 409)
            self.assertEqual(self.app.test_client().post("/reset", headers=self.headers()).status_code, 409)
            self.assertEqual(self.speech.calls, 1)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(results, [200])

    def test_session_capacity_prevents_recognition_cost(self):
        app = create_app(replace(self.settings, sessions_per_device=1),
                         SpeechReplyTurn(self.speech, self.reply))
        client = app.test_client()
        self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertEqual(self.ask(session="other", client=client).status_code, 429)
        self.assertEqual(self.speech.calls, 1)

    def test_full_turn_validates_results_and_releases_failed_empty_sessions(self):
        histories = []
        result = [None]

        def turn(wav, history, scope, remaining, authorize):
            histories.append(list(history))
            return result[0]

        backend = SimpleNamespace(speech_mode="full-turn-speech", reply_mode="full-turn-reply", turn=turn)
        app = create_app(replace(self.settings, sessions_per_device=1), backend)
        client = app.test_client()
        health = client.get("/health", headers=self.headers())
        self.assertEqual(health.json, {"status": "ok", "speech": "full-turn-speech", "reply": "full-turn-reply"})
        self.assertEqual(histories, [])
        for index, (value, status) in enumerate((
                (None, 204), (("", "answer", self.wav), 502),
                (("heard", "x" * 2049, self.wav), 502),
                (("heard", "answer", self.wav[:-1]), 502),
                ((" heard ", " answer ", self.wav), 200))):
            result[0] = value
            self.assertEqual(self.ask(session=f"turn-{index}", client=client).status_code, status)
        self.assertEqual(histories, [[], [], [], [], []])
        self.assertEqual(self.speech.calls, 0)

    def test_no_speech_and_failed_full_turn_preserve_successful_history(self):
        histories = []
        result = [("heard", "answer", self.wav)]

        def turn(wav, history, scope, remaining, authorize):
            histories.append(list(history))
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]

        backend = SimpleNamespace(speech_mode="injected", reply_mode="injected", turn=turn)
        app = create_app(self.settings, backend)
        client = app.test_client()
        self.assertEqual(self.ask(client=client).status_code, 200)
        result[0] = None
        self.assertEqual(self.ask(client=client).status_code, 204)
        result[0] = UpstreamError()
        self.assertEqual(self.ask(client=client).status_code, 502)
        result[0] = ("heard", "answer", self.wav)
        self.assertEqual(self.ask(client=client).status_code, 200)
        expected = [{"role": "user", "content": "heard"}, {"role": "assistant", "content": "answer"}]
        self.assertEqual(histories, [[], expected, expected, expected])

    def test_revocation_after_full_turn_prevents_audio_and_history(self):
        def turn(wav, history, scope, remaining, authorize):
            self.write_registry({"two": TOKENS["two"]})
            return "heard", "answer", self.wav

        backend = SimpleNamespace(speech_mode="injected", reply_mode="injected", turn=turn)
        app = create_app(self.settings, backend)
        response = self.ask(client=app.test_client())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json, {"error": "unauthorized"})
        self.assertNotIn(("one", "session-a"), app.extensions["voice_bridge"].sessions)

    def test_full_turn_deadline_rejects_late_audio_and_releases_the_session(self):
        now = [1000.0]
        delay = [3]

        def turn(wav, history, scope, remaining, authorize):
            now[0] += delay[0]
            return "heard", "answer", self.wav

        backend = SimpleNamespace(speech_mode="injected", reply_mode="injected", turn=turn)
        app = create_app(replace(self.settings, request_timeout=2), backend, clock=lambda: now[0])
        client = app.test_client()
        response = self.ask(client=client)
        self.assertEqual(response.status_code, 504)
        self.assertNotIn(("one", "session-a"), app.extensions["voice_bridge"].sessions)
        delay[0] = 0
        self.assertEqual(self.ask(client=client).status_code, 200)

    def test_success_keeps_unicode_out_of_headers_and_logs(self):
        with self.assertNoLogs(self.app.logger, level="INFO"):
            r = self.ask()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "audio/wav")
        self.assertEqual(r.data, self.wav)
        self.assertEqual(int(r.headers["Content-Length"]), len(self.wav))
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertEqual(self.speech.spoken, ["你好，café."])
        self.assertNotIn("X-Transcript", r.headers)
        self.assertNotIn("X-Reply", r.headers)

    def test_device_and_session_isolation_and_scoped_reset(self):
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.ask("two").status_code, 200)
        self.assertEqual(self.ask(session="session-b").status_code, 200)
        self.assertEqual(self.ask().status_code, 200)
        first, second, third, fourth = self.reply.calls
        self.assertEqual(first[0], [])
        self.assertEqual(second[0], [])
        self.assertEqual(third[0], [])
        self.assertEqual(fourth[0], [{"role": "user", "content": self.speech.heard},
                                    {"role": "assistant", "content": self.reply.reply}])
        self.assertEqual(len({first[2], second[2], third[2]}), 3)
        self.assertEqual(self.client.post("/reset", headers=self.headers()).status_code, 200)
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [])
        self.assertEqual(self.ask("two").status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], fourth[0])

    def test_revocation_and_rotation_take_effect_without_restart(self):
        self.assertEqual(self.ask().status_code, 200)
        self.write_registry({"two": TOKENS["two"]})
        self.assertEqual(self.ask().status_code, 401)
        self.assertEqual(self.ask("two").status_code, 200)
        self.write_registry({"one": "c" * 43, "two": TOKENS["two"]})
        self.assertEqual(self.ask().status_code, 401)
        headers = {**self.headers(), "Authorization": "Bearer " + "c" * 43}
        self.assertEqual(self.client.post("/ask", data=self.wav, headers=headers).status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [])

    def test_rate_limit_is_device_wide_not_session_or_header_controlled(self):
        app = create_app(replace(self.settings, requests_per_minute=2), SpeechReplyTurn(self.speech, self.reply))
        client = app.test_client()
        self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertEqual(self.ask(session="new", client=client).status_code, 200)
        r = self.ask(session="another", client=client)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(self.ask("two", client=client).status_code, 200)

    def test_timeout_failure_and_invalid_reply_do_not_commit_history(self):
        for failure, status in ((UpstreamTimeout(), 504), (UpstreamError(), 502)):
            self.reply.failure = failure
            self.assertEqual(self.ask().status_code, status)
        self.reply.failure = None
        self.reply.reply = "x" * 2049
        self.assertEqual(self.ask().status_code, 502)
        self.reply.reply = "ok"
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [])
        self.speech.failure = UpstreamError()
        self.assertEqual(self.ask().status_code, 502)

    def test_invalid_tts_and_oversized_responses_are_not_sent(self):
        self.speech.synthesize = lambda text, timeout: self.wav[:-1]
        self.assertEqual(self.ask().status_code, 502)
        self.speech.synthesize = lambda text, timeout: audio(seconds=30.1)
        self.assertEqual(self.ask().status_code, 502)

    def test_unsupported_reply_rates_release_no_audio_or_history(self):
        for rate in (8000, 22050, 24000, 44100, 48000):
            with self.subTest(rate=rate):
                self.speech.synthesize = lambda text, timeout: audio(rate=rate)
                response = self.ask()
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.mimetype, "application/json")
                self.assertEqual(response.json, {"error": "upstream_unavailable"})
                self.assertNotIn(b"RIFF", response.data)
                self.assertNotIn(("one", "session-a"), self.app.extensions["voice_bridge"].sessions)
        self.speech.synthesize = lambda text, timeout: self.wav
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "Optional local speech requires FFmpeg")
    def test_local_resampling_reaches_consumer_with_pitch_and_timing_preserved(self):
        for rate in (22050, 24000):
            with self.subTest(rate=rate):
                # A 1 kHz burst starts at 250 ms and stops at 750 ms. Relabeling
                # its source PCM as 16 kHz changes both markers and its pitch.
                samples = [int(10000 * math.sin(2 * math.pi * 1000 * i / rate))
                           if rate // 4 <= i < rate * 3 // 4 else 0 for i in range(rate)]
                source = Path(self.tmp.name) / "piper.wav"
                source.write_bytes(make_wav(1, rate, 16, struct.pack(f"<{rate}h", *samples)))
                self.speech.synthesize = lambda text, timeout: piper_reply_wav(source)
                response = self.ask()
                self.assertEqual(response.status_code, 200)
                channels, board_rate, bits, pcm = parse_wav(response.data, output=True)
                self.assertEqual((channels, board_rate, bits), (1, 16000, 16))
                self.assertEqual(len(response.data), 44 + len(pcm))
                self.assertLessEqual(abs(len(pcm) // 2 - 16000), 1)
                actual = [sample[0] for sample in struct.iter_unpack("<h", pcm)]
                active = [i for i, sample in enumerate(actual) if abs(sample) > 1000]
                self.assertAlmostEqual(active[0] / board_rate, 0.25, delta=0.002)
                self.assertAlmostEqual(active[-1] / board_rate, 0.75, delta=0.002)
                middle = actual[4800:11200]  # 400 ms clear of filter transients.
                crossings = sum(a <= 0 < b for a, b in zip(middle, middle[1:]))
                self.assertAlmostEqual(crossings / (len(middle) / board_rate), 1000, delta=3)
                rms = math.sqrt(sum(sample * sample for sample in middle) / len(middle))
                self.assertAlmostEqual(rms, 10000 / math.sqrt(2), delta=150)

    def test_concurrent_session_is_rejected_and_global_capacity_is_bounded(self):
        started, release = threading.Event(), threading.Event()
        self.reply.started, self.reply.release = started, release
        app = create_app(replace(self.settings, max_concurrent=1), SpeechReplyTurn(self.speech, self.reply))
        results = []
        thread = threading.Thread(target=lambda: results.append(self.ask(client=app.test_client()).status_code))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.ask("two", client=app.test_client()).status_code, 503)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(results, [200])
        self.assertEqual(self.ask("two", client=app.test_client()).status_code, 200)

    def test_session_capacity_and_history_retention(self):
        settings = replace(self.settings, sessions_per_device=1, requests_per_minute=20, history_turns=1)
        app = create_app(settings, SpeechReplyTurn(self.speech, self.reply))
        client = app.test_client()
        for _ in range(3):
            self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [{"role": "user", "content": self.speech.heard},
                                                 {"role": "assistant", "content": self.reply.reply}])
        self.assertEqual(self.ask(session="new", client=client).status_code, 429)
        self.assertEqual(self.ask("two", client=client).status_code, 200)

    def test_legacy_routes_are_not_available(self):
        self.assertEqual(self.client.get("/hello", headers=self.headers()).status_code, 404)

    def test_idle_and_absolute_retention_rotate_private_scope(self):
        now = [1000.0]
        app = create_app(replace(self.settings, idle_seconds=10, max_age_seconds=25, requests_per_minute=20),
                         SpeechReplyTurn(self.speech, self.reply), clock=lambda: now[0])
        client = app.test_client()
        self.assertEqual(self.ask(client=client).status_code, 200)
        first_scope = self.reply.calls[-1][2]
        now[0] += 11
        self.assertEqual(self.ask(client=client).status_code, 200)
        second_scope = self.reply.calls[-1][2]
        self.assertNotEqual(first_scope, second_scope)
        self.assertEqual(self.reply.calls[-1][0], [])
        for _ in range(5):
            now[0] += 6
            self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertNotEqual(second_scope, self.reply.calls[-1][2])
        self.assertEqual(self.reply.calls[-1][0], [])

    def test_busy_session_reset_and_revocation_during_upstream_call(self):
        started, release = threading.Event(), threading.Event()
        self.reply.started, self.reply.release = started, release
        results = []
        thread = threading.Thread(target=lambda: results.append(self.ask(client=self.app.test_client()).status_code))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertEqual(self.ask(client=self.app.test_client()).status_code, 409)
            self.assertEqual(self.app.test_client().post("/reset", headers=self.headers()).status_code, 409)
            self.write_registry({"two": TOKENS["two"]})
            self.assertEqual(self.app.test_client().get("/health", headers=self.headers()).status_code, 401)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(results, [401])
        self.assertNotIn(("one", "session-a"), self.app.extensions["voice_bridge"].sessions)

    def test_rate_window_recovers_and_health_has_its_own_limit(self):
        now = [1000.0]
        app = create_app(replace(self.settings, requests_per_minute=1, health_per_minute=1),
                         SpeechReplyTurn(self.speech, self.reply), clock=lambda: now[0])
        client = app.test_client()
        self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertEqual(client.get("/health", headers=self.headers()).status_code, 200)
        self.assertEqual(client.get("/health", headers=self.headers()).status_code, 429)
        self.assertEqual(self.ask(client=client).status_code, 429)
        now[0] += 60
        self.assertEqual(self.ask(client=client).status_code, 200)
        self.assertEqual(client.get("/health", headers=self.headers()).status_code, 200)

    def test_unavailable_or_public_registry_fails_closed(self):
        self.registry.chmod(0o644)
        self.assertEqual(self.client.get("/health", headers=self.headers()).status_code, 503)
        self.registry.unlink()
        self.assertEqual(self.ask().status_code, 503)

    def test_rotation_during_a_turn_preserves_the_new_credentials_conversation(self):
        started, release = threading.Event(), threading.Event()

        def chat(history, text, scope, timeout):
            self.reply.calls.append((list(history), text, scope))
            if len(self.reply.calls) == 1:
                started.set()
                release.wait(3)
            return self.reply.reply

        self.reply.chat = chat
        results = []
        thread = threading.Thread(target=lambda: results.append(self.ask(client=self.app.test_client()).status_code))
        thread.start()
        headers = {**self.headers(), "Authorization": "Bearer " + "c" * 43}
        try:
            self.assertTrue(started.wait(2))
            self.write_registry({"one": "c" * 43, "two": TOKENS["two"]})
            self.assertEqual(self.app.test_client().post("/ask", data=self.wav, headers=headers).status_code, 200)
        finally:
            release.set()
            thread.join(4)
        self.assertEqual(results, [401])
        self.assertEqual(self.client.post("/ask", data=self.wav, headers=headers).status_code, 200)
        self.assertEqual(self.reply.calls[-1][0], [{"role": "user", "content": self.speech.heard},
                                                 {"role": "assistant", "content": self.reply.reply}])

    def test_revocation_after_recognition_prevents_new_reply_cost(self):
        def transcribe(wav, timeout):
            self.write_registry({"two": TOKENS["two"]})
            return self.speech.heard
        self.speech.transcribe = transcribe
        self.assertEqual(self.ask().status_code, 401)
        self.assertEqual(self.reply.calls, [])
        self.assertEqual(self.speech.spoken, [])

    def test_revocation_after_reply_prevents_new_synthesis_cost(self):
        def chat(history, text, scope, timeout):
            self.write_registry({"two": TOKENS["two"]})
            return self.reply.reply
        self.reply.chat = chat
        self.assertEqual(self.ask().status_code, 401)
        self.assertEqual(self.speech.spoken, [])

    def test_combined_deadline_cannot_exceed_firmware_budget(self):
        with self.assertRaises(ValueError):
            replace(self.settings, request_timeout=91)


if __name__ == "__main__":
    unittest.main()
