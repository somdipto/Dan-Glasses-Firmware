"""Real adapter HTTP/TLS against disposable dummy providers; no saved credentials or live calls."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from server.audio import make_wav, parse_wav
from server.backends import ChatHTTP, MockSpeech, UpstreamError, UpstreamTimeout, configured_backends, credential_origin
from server.server import Settings, create_app
from server.turns import SpeechReplyTurn
from tools.provider_fixture import REPLY, TRANSCRIPT, start_provider

ROOT = Path(__file__).resolve().parent.parent


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cleanup = ExitStack()
        cls.addClassCleanup(cls.cleanup.close)
        cls.home = Path(cls.cleanup.enter_context(tempfile.TemporaryDirectory(prefix="glasses-provider-contract-")))
        cls.secrets = cls.home / "secrets"
        subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(cls.secrets), "local-tls"],
                       cwd=ROOT, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=35,
                       env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(cls.home),
                            "TMPDIR": str(cls.home), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        cls.apis = {role: start_provider(cls.home, cls.secrets, role, cls.cleanup) for role in ("speech", "chat", "search")}
        cls.ca = str(cls.secrets / "localhost.crt")

    def setUp(self):
        for api in self.apis.values():
            api.chat_path, api.same_origin_search = "/v1/chat/completions", False
            api.mode, api.arguments, api.last_text = "ok", None, None
            api.calls, api.rejected = [], 0
            Path(api.key_file).write_text(api.key)
            Path(api.key_file).chmod(0o600)
        speech, chat, search = (self.apis[role] for role in ("speech", "chat", "search"))
        self.values = {"SPEECH_BACKEND": "cartesia", "CARTESIA_BASE_URL": speech.origin,
                       "CARTESIA_KEY_FILE": speech.key_file, "CARTESIA_STT_MODEL": "ink-whisper",
                       "CARTESIA_TTS_MODEL": "sonic-3.6", "CARTESIA_VOICE_ID": "fixture-voice",
                       "CARTESIA_LANGUAGE": "en", "CARTESIA_VERSION": "2026-08-14", "CARTESIA_CA_FILE": self.ca,
                       "REPLY_BACKEND": "direct", "LLM_BASE_URL": chat.origin + "/v1",
                       "LLM_KEY_FILE": chat.key_file, "LLM_MODEL": "fixture-model", "LLM_API_STYLE": "deepseek",
                       "LLM_CA_FILE": self.ca, "WEB_SEARCH_BACKEND": "deepseek", "WEB_SEARCH_BASE_URL": search.origin,
                       "WEB_SEARCH_KEY_FILE": search.key_file, "WEB_SEARCH_CA_FILE": self.ca}
        self.speech, self.reply = self.adapters()
        self.wav = MockSpeech().synthesize("fixture", 1)

    def adapters(self, **changes):
        with patch.dict(os.environ, {**self.values, **changes}, clear=True):
            return configured_backends()

    def client(self):
        registry = self.home / "devices.json"
        registry.write_text(json.dumps({"fixture": hashlib.sha256(("a" * 43).encode()).hexdigest()}))
        registry.chmod(0o600)
        app = create_app(Settings(devices_file=registry), SpeechReplyTurn(self.speech, self.reply))
        return app, app.test_client()

    def ask(self, client):
        return client.post("/ask", data=self.wav, headers={"Authorization": "Bearer " + "a" * 43,
                           "Content-Type": "audio/wav", "X-Session": "provider-fixture"})

    def test_voice_turn_executes_real_transport_and_commits_only_final_dialogue(self):
        app, client = self.client()
        result = self.ask(client)
        self.assertEqual(result.status_code, 200)
        channels, rate, bits, pcm = parse_wav(result.data, output=True)
        self.assertEqual((channels, rate, bits, len(pcm)), (1, 16000, 16, 16000))
        self.assertEqual(self.apis["speech"].last_text, REPLY)
        self.assertEqual(self.apis["search"].calls, ["/v1/tools/search"])
        history = list(app.extensions["voice_bridge"].sessions[("fixture", "provider-fixture")].messages)
        self.assertEqual(history, [{"role": "user", "content": TRANSCRIPT}, {"role": "assistant", "content": REPLY}])
        self.assertNotIn(TRANSCRIPT, str(result.headers))
        self.assertNotIn(REPLY, str(result.headers))

    def test_streamed_cartesia_audio_becomes_a_finite_pcm_wav(self):
        self.apis["speech"].mode = "streaming-wav"
        _, client = self.client()
        result = self.ask(client)
        self.assertEqual(result.status_code, 200)
        channels, rate, bits, pcm = parse_wav(result.data, output=True)
        self.assertEqual((channels, rate, bits), (1, 16000, 16))
        self.assertEqual(pcm, b"\x80\x00" * 8000)
        self.assertEqual(len(result.data), 44 + len(pcm))

    def test_bad_input_is_rejected_before_sending_audio(self):
        for wav in (b"bad WAV", make_wav(2, 16000, 16, b"\0\0" * 16000),
                    make_wav(1, 16000, 16, b"\0\0" * 1600), make_wav(1, 16000, 16, b"\0\0" * 160001),
                    b"x" * 330001):
            with self.subTest(size=len(wav)), self.assertRaises(ValueError):
                self.speech.transcribe(wav, 5)
        self.assertEqual(self.apis["speech"].calls, [])

    def test_bad_tts_format_and_response_limits_fail_without_returning_audio(self):
        for mode in ("empty-pcm", "short-pcm", "odd-pcm", "over-duration-pcm", "wav-instead-of-pcm",
                     "oversized", "failure", "redirect", "encoded"):
            self.apis["speech"].mode = mode
            with self.subTest(mode=mode), self.assertRaises(UpstreamError):
                self.speech.synthesize("Bounded fixture.", 5)

    def test_pcm_duration_boundaries_produce_valid_canonical_wav(self):
        for mode, samples in (("minimum-pcm", 320), ("maximum-pcm", 480000)):
            self.apis["speech"].mode = mode
            with self.subTest(mode=mode):
                wav = self.speech.synthesize("Bounded fixture.", 5)
                channels, rate, bits, pcm = parse_wav(wav, output=True)
                self.assertEqual((channels, rate, bits, len(pcm)), (1, 16000, 16, samples * 2))
                self.assertEqual(len(wav), 44 + len(pcm))


    def test_stt_silence_and_provider_failure_stop_later_stages(self):
        _, client = self.client()
        self.apis["speech"].mode = "empty"
        self.assertEqual(self.ask(client).status_code, 204)
        self.assertEqual(self.apis["speech"].calls, ["/stt"])
        self.assertEqual(self.apis["chat"].calls, [])
        for mode in ("bad-transcript", "invalid-json", "oversized", "failure"):
            self.apis["speech"].mode = mode
            with self.subTest(mode=mode):
                result = self.ask(client)
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json, {"error": "upstream_unavailable"})
        self.assertEqual(self.apis["chat"].calls, [])

    def test_unknown_tools_and_bad_arguments_are_rejected_before_any_search(self):
        self.apis["chat"].mode = "unknown-tool"
        with self.assertRaises(UpstreamError):
            self.reply.chat([], "fixture", "scope", 5)
        self.assertEqual(self.apis["search"].calls, [])
        self.apis["chat"].mode = "ok"
        for arguments in ('[]', '[["query","x"]]', '{"query":"a","query":"b"}',
                          '{"query":"x","url":"https://not-allowed.test"}', '{"query":0}',
                          '{"query":" "}', json.dumps({"query": "x" * 129}), 'not JSON'):
            self.apis["chat"].arguments = arguments
            with self.subTest(arguments=arguments), self.assertRaises(UpstreamError):
                self.reply.chat([], "fixture", "scope", 5)
            self.assertEqual(self.apis["search"].calls, [])

    def test_tool_budget_duplicate_ids_and_disabled_tools_fail_closed(self):
        self.apis["chat"].mode = "endless"
        with self.assertRaises(UpstreamError):
            self.reply.chat([], "fixture", "scope", 5)
        self.assertEqual(len(self.apis["search"].calls), 2)
        self.assertEqual(len(self.apis["chat"].calls), 3)
        self.apis["chat"].mode = "duplicate-id"
        self.apis["search"].calls = []
        with self.assertRaises(UpstreamError):
            self.reply.chat([], "fixture", "scope", 5)
        self.assertEqual(len(self.apis["search"].calls), 1)

    def test_parallel_tool_batch_is_prevalidated_and_result_sizes_are_bounded(self):
        self.apis["chat"].mode = "batch"
        self.apis["search"].mode = "long-summary"
        self.assertEqual(self.reply.chat([], "fixture", "scope", 5), REPLY)
        self.assertEqual(len(self.apis["search"].calls), 2)
        self.assertEqual(len(self.apis["chat"].calls), 2)

    def test_search_errors_never_become_fake_answers(self):
        _, client = self.client()
        for mode in ("failure", "invalid-json", "bad-results", "bad-url", "oversized"):
            self.apis["search"].mode = mode
            with self.subTest(mode=mode):
                result = self.ask(client)
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json, {"error": "upstream_unavailable"})
                self.assertEqual(self.apis["speech"].last_text, None)
        self.apis["search"].mode = "empty"
        self.assertEqual(self.ask(client).status_code, 200)

    def test_tls_trust_and_hostname_fail_for_speech_and_search(self):
        for changes in ({"CARTESIA_CA_FILE": ""},
                        {"CARTESIA_BASE_URL": self.apis["speech"].origin.replace("localhost", "127.0.0.1")}):
            speech, _ = self.adapters(**changes)
            with self.subTest(config=changes), self.assertRaises(UpstreamError):
                speech.transcribe(self.wav, 5)
        self.assertEqual(self.apis["speech"].calls, [])
        for changes in ({"WEB_SEARCH_CA_FILE": ""},
                        {"WEB_SEARCH_BASE_URL": self.apis["search"].origin.replace("localhost", "127.0.0.1")}):
            _, reply = self.adapters(**changes)
            with self.subTest(config=changes), self.assertRaises(UpstreamError):
                reply.chat([], "fixture", "scope", 5)
        self.assertEqual(self.apis["search"].calls, [])

    def test_gateway_key_cannot_cross_search_origin_even_after_rotation(self):
        with self.assertRaises(ValueError):
            self.adapters(WEB_SEARCH_KEY_FILE=self.apis["chat"].key_file)
        Path(self.apis["search"].key_file).write_text(self.apis["chat"].key)
        with self.assertRaises(UpstreamError):
            self.reply.chat([], "fixture", "scope", 5)
        self.assertEqual(self.apis["chat"].calls, [])
        self.assertEqual(self.apis["search"].calls, [])

    def test_path_prefixed_chat_base_preserves_route_with_independent_search(self):
        self.apis["chat"].chat_path = "/api/v1/chat/completions"
        # The review's public-looking configuration is construction-only; never contacted.
        ChatHTTP("https://gateway.example/api/v1", self.apis["chat"].key_file, "fixture-model",
                 api_style="deepseek", search={"origin": "https://api.deepseek.com",
                 "key_file": self.apis["search"].key_file, "ca_file": None})
        _, reply = self.adapters(LLM_BASE_URL=self.apis["chat"].origin + "/api/v1")
        self.assertEqual(reply.chat([], "fixture", "scope", 5), REPLY)
        self.assertEqual(self.apis["chat"].calls, ["/api/v1/chat/completions"] * 2)
        self.assertEqual(self.apis["search"].calls, ["/v1/tools/search"])

    def test_same_effective_origin_allows_direct_credential_in_constructor_and_worker(self):
        adapter = ChatHTTP("https://API.DEEPSEEK.COM:443/api/v1", self.apis["chat"].key_file,
                           "fixture-model", api_style="deepseek", search={"origin": "https://api.deepseek.com",
                           "key_file": self.apis["chat"].key_file, "ca_file": None})
        self.assertEqual(credential_origin(adapter.base_url), ("https", "api.deepseek.com", 443))
        self.apis["chat"].same_origin_search = True
        _, reply = self.adapters(WEB_SEARCH_BASE_URL=self.apis["chat"].origin.replace("localhost", "LOCALHOST"),
                                 WEB_SEARCH_KEY_FILE=self.apis["chat"].key_file)
        self.assertEqual(reply.chat([], "fixture", "scope", 5), REPLY)
        self.assertEqual(self.apis["chat"].calls, ["/v1/chat/completions", "/v1/tools/search", "/v1/chat/completions"])
        self.assertEqual(self.apis["search"].calls, [])

    def test_private_credential_controls_and_symlinks_fail_before_network(self):
        path = Path(self.apis["speech"].key_file)
        for value in (b"invalid" + bytes([0]) + b"key", b"invalid" + bytes([127]) + b"key", b"x" * 4099):
            path.write_bytes(value)
            with self.subTest(kind=value[:8]), self.assertRaises(ValueError):
                self.adapters()
        path.write_text(self.apis["speech"].key)
        linked = self.home / "linked.key"
        linked.symlink_to(path)
        for variable in ("CARTESIA_KEY_FILE", "WEB_SEARCH_KEY_FILE"):
            with self.subTest(variable=variable), self.assertRaises(ValueError):
                self.adapters(**{variable: str(linked)})
        self.assertTrue(all(not api.calls for api in self.apis.values()))

    def test_missing_unverified_or_mismatched_configuration_never_selects_fallback(self):
        for variable in ("CARTESIA_BASE_URL", "CARTESIA_VOICE_ID", "LLM_MODEL", "LLM_API_STYLE", "WEB_SEARCH_KEY_FILE"):
            with self.subTest(missing=variable), patch.dict(os.environ, {key: value for key, value in self.values.items() if key != variable}, clear=True):
                with self.assertRaises(KeyError):
                    configured_backends()
        for changes in ({"CARTESIA_BASE_URL": "http://api.cartesia.ai"},
                        {"CARTESIA_BASE_URL": "https://not-cartesia.test"},
                        {"CARTESIA_STT_MODEL": "ink-2"}, {"CARTESIA_VERSION": "2000-01-01"},
                        {"WEB_SEARCH_BASE_URL": "https://platform.yeta.ai"}, {"REPLY_BACKEND": "mock"},
                        {"LLM_API_STYLE": "unverified"}):
            with self.subTest(config=changes), self.assertRaises(ValueError):
                self.adapters(**changes)
        self.assertTrue(all(not api.calls for api in self.apis.values()))

    def test_deadlines_terminate_speech_and_search_and_capacity_recovers(self):
        for role, operation in (("speech", lambda: self.speech.transcribe(self.wav, 0.3)),
                                ("search", lambda: self.reply.chat([], "fixture", "scope", 0.6))):
            self.apis[role].mode = "slow"
            started = time.monotonic()
            with self.subTest(role=role), self.assertRaises(UpstreamTimeout):
                operation()
            self.assertLess(time.monotonic() - started, 2)
            self.apis[role].mode = "ok"
        self.assertEqual(self.reply.chat([], "fixture", "scope", 5), REPLY)

    def test_normal_mode_contract_rejects_unexpected_reasoning(self):
        self.apis["chat"].mode = "thinking"
        _, reply = self.adapters(WEB_SEARCH_BACKEND="disabled")
        with self.assertRaises(UpstreamError):
            reply.chat([], "fixture", "scope", 5)


if __name__ == "__main__":
    unittest.main()
