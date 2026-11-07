"""Launch an isolated loopback TLS bridge, exercise it, then remove fixture data."""
import argparse
import io
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import requests
from server.backends import LocalSpeech, MockSpeech
from server.audio import make_wav, parse_wav
from tools.provider_fixture import REPLY, TRANSCRIPT, start_provider


class DirectAPIFixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + self.server.key:
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            self.send_error(413)
            return
        body = json.loads(self.rfile.read(length))
        if (body.get("model") != "fixture-model" or body.get("max_tokens") != 256
                or "store" in body or body.get("stream") is not False
                or "tools" in body or any(name.lower().startswith("x-hermes-") for name in self.headers)
                or self.server.keyword not in body["messages"][-1]["content"].lower()):
            self.send_error(400)
            return
        self.server.calls += 1
        raw = json.dumps({"choices": [{"message": {"role": "assistant",
                         "content": "The direct API call works. Hermes is not required."}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def smoke(local_speech, direct_fixture=False, provider_fixture=False):
    with tempfile.TemporaryDirectory(prefix="glasses-tls-fixture-") as directory, ExitStack() as cleanup:
        home = Path(directory)
        secrets = home / "secrets"
        for command in (("local-tls",), ("create-device", "fixture")):
            subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(secrets), *command],
                           cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
        token = (secrets / "fixture.token").read_text().strip()
        ca = str(secrets / "localhost.crt")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "PYTHONUTF8": "1",
               "BRIDGE_DEVICES_FILE": str(secrets / "devices.json"), "BRIDGE_TLS_CERT": ca,
               "BRIDGE_TLS_KEY": str(secrets / "localhost.key"), "VOICE_BACKEND": "pipeline",
               "SPEECH_BACKEND": "mock", "REPLY_BACKEND": "mock"}
        if provider_fixture:
            speech_api = start_provider(home, secrets, "speech", cleanup)
            chat_api = start_provider(home, secrets, "chat", cleanup)
            search_api = start_provider(home, secrets, "search", cleanup)
            env.update(SPEECH_BACKEND="cartesia", CARTESIA_BASE_URL=speech_api.origin,
                       CARTESIA_KEY_FILE=speech_api.key_file, CARTESIA_STT_MODEL="ink-whisper",
                       CARTESIA_TTS_MODEL="sonic-3.6", CARTESIA_VOICE_ID="fixture-voice",
                       CARTESIA_LANGUAGE="en", CARTESIA_VERSION="2026-08-14", CARTESIA_CA_FILE=ca,
                       REPLY_BACKEND="direct", LLM_BASE_URL=chat_api.origin + "/v1",
                       LLM_KEY_FILE=chat_api.key_file, LLM_MODEL="fixture-model", LLM_API_STYLE="deepseek",
                       LLM_CA_FILE=ca, WEB_SEARCH_BACKEND="deepseek", WEB_SEARCH_BASE_URL=search_api.origin,
                       WEB_SEARCH_KEY_FILE=search_api.key_file, WEB_SEARCH_CA_FILE=ca)
        if direct_fixture:
            provider_key = os.urandom(32).hex()
            key_file = home / "provider.key"
            key_file.write_text(provider_key)
            key_file.chmod(0o600)
            api = ThreadingHTTPServer(("127.0.0.1", 0), DirectAPIFixture)
            api.daemon_threads = True
            api.key, api.calls, api.keyword = provider_key, 0, "bridge" if local_speech else "mock"
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(ca, secrets / "localhost.key")
            api.socket = context.wrap_socket(api.socket, server_side=True)
            thread = threading.Thread(target=api.serve_forever, daemon=True)
            thread.start()
            cleanup.callback(thread.join, 2)
            cleanup.callback(api.server_close)
            cleanup.callback(api.shutdown)
            env.update(REPLY_BACKEND="direct", LLM_BASE_URL=f"https://localhost:{api.server_port}/v1",
                       LLM_KEY_FILE=str(key_file), LLM_MODEL="fixture-model", LLM_CA_FILE=ca, LLM_API_STYLE="openai")
        if local_speech:
            binary = shutil.which("whisper-cli")
            if not binary:
                raise RuntimeError("Install the pinned whisper.cpp CLI before local speech smoke")
            env.update(SPEECH_BACKEND="local", WHISPER_BIN=binary,
                       WHISPER_MODEL=str(ROOT / "models/ggml-tiny.en-q5_1.bin"),
                       PIPER_MODEL=str(ROOT / "models/en_US-ljspeech-medium.onnx"))
            speech = LocalSpeech(binary, env["WHISPER_MODEL"], env["PIPER_MODEL"])
            wav = speech.synthesize("The local voice bridge is ready.", 30)
            channels, rate, bits, pcm = parse_wav(wav, output=True)
            assert (channels, rate, bits) == (1, 16000, 16)
            # The real producer, not a smoke-only resampler, must satisfy the
            # microphone/board format. Keep the bounded STT input at ten seconds.
            wav = make_wav(1, rate, bits, pcm[:rate * 2 * 10])
            heard = speech.transcribe(wav, 30)
            assert "bridge" in heard.lower(), "Local STT did not recognize the generated test phrase"
            print("Real local Piper synthesis and Whisper recognition passed; no transcript printed.")
        else:
            wav = MockSpeech().synthesize("fixture", 1)
        parse_wav(wav)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env["BRIDGE_PORT"] = str(port)
        url = f"https://localhost:{port}"
        client = requests.Session()
        client.trust_env = False
        headers = {"Authorization": "Bearer " + token, "X-Session": "tls-fixture"}
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen([sys.executable, "-m", "server.server"], cwd=ROOT, env=env,
                                       stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 8
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Isolated bridge exited before readiness")
                    try:
                        health = client.get(url + "/health", headers=headers, verify=ca, timeout=(1, 2), allow_redirects=False)
                        if health.status_code == 200:
                            break
                    except requests.ConnectionError:
                        pass
                    if time.monotonic() > deadline:
                        raise RuntimeError("Isolated TLS bridge readiness timed out")
                    time.sleep(0.05)
                expected_reply = ("direct-chat-completions-http" if direct_fixture or provider_fixture else "mock-not-an-llm")
                assert health.json()["reply"] == expected_reply
                if direct_fixture:
                    assert api.calls == 0, "Health unexpectedly called the LLM API"
                if provider_fixture:
                    assert health.json()["speech"] == "cartesia-batch-https"
                    assert speech_api.calls == chat_api.calls == search_api.calls == []
                assert client.get(url + "/health", verify=ca, timeout=3).status_code == 401
                assert client.get(url + "/health", headers={"Authorization": "Bearer " + "z" * 43}, verify=ca, timeout=3).status_code == 401
                for target, trust in ((url, True), (f"https://127.0.0.1:{port}", ca)):
                    try:
                        client.get(target + "/health", headers=headers, verify=trust, timeout=3)
                    except requests.exceptions.SSLError:
                        pass
                    else:
                        raise AssertionError("Invalid CA or hostname was accepted")
                subprocess.run([sys.executable, "tools/test_server.py", "--url", url, "--token-file",
                                str(secrets / "fixture.token"), "--ca-file", ca], cwd=ROOT, env=env, check=True, timeout=15)
                start = time.monotonic()
                response = client.post(url + "/ask", data=wav, headers={**headers, "Content-Type": "audio/wav"},
                                       verify=ca, timeout=(5, 95), allow_redirects=False)
                assert response.status_code == 200, "WAV round trip failed"
                assert response.headers["Content-Type"] == "audio/wav"
                assert "X-Transcript" not in response.headers and "X-Reply" not in response.headers
                channels, rate, bits, pcm = parse_wav(response.content, output=True)
                assert (channels, rate, bits) == (1, 16000, 16)
                with wave.open(io.BytesIO(response.content)) as output:
                    assert (output.getnchannels(), output.getsampwidth(), output.getframerate()) == (1, 2, 16000)
                    assert output.readframes(output.getnframes()) == pcm
                print(f"Verified HTTPS WAV round trip: {len(response.content)} bytes, {len(pcm)/(rate*2):.2f}s audio, {time.monotonic()-start:.2f}s latency.")
                silent = make_wav(1, 16000, 16, b"\0\0" * 8000)
                assert client.post(url + "/ask", data=silent, headers={**headers, "Content-Type": "audio/wav"}, verify=ca, timeout=5).status_code == 204
                assert client.post(url + "/ask", data=b"not WAV", headers={**headers, "Content-Type": "audio/wav"}, verify=ca, timeout=5).status_code == 422
                assert client.post(url + "/ask", data=b"x" * 330001, headers={**headers, "Content-Type": "audio/wav"}, verify=ca, timeout=5).status_code == 413
                subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(secrets), "rotate-device", "fixture"],
                               cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                assert client.get(url + "/health", headers=headers, verify=ca, timeout=3).status_code == 401
                new_token = (secrets / "fixture.token").read_text().strip()
                assert new_token != token
                headers["Authorization"] = "Bearer " + new_token
                assert client.get(url + "/health", headers=headers, verify=ca, timeout=3).status_code == 200
                subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(secrets), "revoke-device", "fixture"],
                               cwd=ROOT, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                assert client.get(url + "/health", headers=headers, verify=ca, timeout=3).status_code == 401
                print("CA/hostname rejection, token-gated health, silence, invalid/oversized WAV, rotation and revocation passed.")
                if direct_fixture:
                    assert api.calls == 1, "Unexpected LLM call count or fallback"
                    print("Direct Chat Completions API path passed over verified HTTPS; Hermes was not used.")
                if provider_fixture:
                    assert speech_api.calls == ["/stt", "/tts/bytes"] and speech_api.last_text == REPLY
                    assert chat_api.calls == ["/v1/chat/completions"] * 2
                    assert search_api.calls == ["/v1/tools/search"]
                    assert speech_api.rejected == chat_api.rejected == search_api.rejected == 0
                    print("Cartesia STT/TTS, normal-mode replies, allowlisted tool/result loop and search transport passed across separate verified TLS fixture origins.")
            finally:
                client.close()
                os.killpg(process.pid, signal.SIGTERM) if process.poll() is None else None
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(5)
                log.seek(0)
                captured = log.read(65536)
                assert token.encode() not in captured and headers["Authorization"].split(" ", 1)[1].encode() not in captured, "Credential leaked into fixture log"
                if local_speech:
                    assert heard.encode("utf-8") not in captured, "Transcript leaked into fixture log"
                if direct_fixture:
                    assert provider_key.encode() not in captured, "Provider credential leaked into fixture log"
                if provider_fixture:
                    assert TRANSCRIPT.encode() not in captured and REPLY.encode() not in captured
                    assert all(api.key.encode() not in captured for api in (speech_api, chat_api, search_api))
        print("Fixture stopped and private temporary data removed. No hardware or live providers were contacted.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-speech", action="store_true", help="exercise real offline Whisper/Piper")
    parser.add_argument("--direct-fixture", action="store_true", help="exercise the direct LLM adapter against an isolated TLS mock API, not a paid provider")
    parser.add_argument("--provider-fixture", action="store_true", help="exercise Cartesia + normal-mode chat + web search adapters against separate dummy HTTPS origins; no real keys")
    parser.add_argument("--stream-fixture", action="store_true", help="exercise native Cartesia-only live WSS against an isolated dummy provider; no real keys")
    args = parser.parse_args()
    if args.provider_fixture and (args.local_speech or args.direct_fixture):
        parser.error("--provider-fixture is separate from the offline-speech/direct-only modes")
    if args.stream_fixture and (args.local_speech or args.direct_fixture or args.provider_fixture):
        parser.error("--stream-fixture is separate from pipeline provider/offline modes")
    if args.stream_fixture:
        from tools.smoke_stream import smoke as stream_smoke
        stream_smoke()
    else:
        smoke(args.local_speech, args.direct_fixture, args.provider_fixture)
