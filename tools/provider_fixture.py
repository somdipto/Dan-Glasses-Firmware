"""Dummy Cartesia/chat/search HTTP contracts on separate loopback TLS origins. Never live providers."""
import json
import ssl
import threading
import time
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server.audio import make_wav, parse_wav
from tools.credentials import private_write

TRANSCRIPT = "Search the public web for café."
REPLY = "The isolated search found the public documentation."


class ProviderAPI(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + self.server.key:
            self.server.rejected += 1
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 460000:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        mode = self.server.mode
        if self.server.role == "speech" and self.headers.get("Cartesia-Version") != "2026-08-14":
            self.send_error(400)
            return
        if self.path == "/stt" and self.server.role == "speech":
            message = BytesParser().parsebytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
            parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                     for part in message.get_payload()}
            parse_wav(parts["file"])
            assert parts["model"] == b"ink-whisper" and parts["language"] == b"en"
            body = {"type": "transcript", "text": "" if mode == "empty" else TRANSCRIPT}
            if mode == "bad-transcript":
                body["text"] = ["not a transcript string"]
            content_type = "application/json"
        else:
            body = json.loads(raw)
            if self.path == "/tts/bytes" and self.server.role == "speech":
                assert body["model_id"] == "sonic-3.6" and body["voice"] == {"id": "fixture-voice"}
                assert body["language"] == "en"
                output_format = body["output_format"]
                expected_format = {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}
                if mode == "streaming-wav":
                    assert output_format in (expected_format, {**expected_format, "container": "wav"})
                else:
                    assert output_format == expected_format
                self.server.last_text = body["transcript"]
                samples = 480000 if mode == "maximum-pcm" else 320 if mode == "minimum-pcm" else 8000
                body = b"\x80\x00" * samples
                content_type = "audio/pcm"
                if mode == "empty-pcm":
                    body = b""
                elif mode == "short-pcm":
                    body = body[:638]
                elif mode == "odd-pcm":
                    body = body[:-1]
                elif mode == "over-duration-pcm":
                    body = b"\x80\x00" * 480001
                elif mode == "wav-instead-of-pcm" or output_format["container"] == "wav":
                    body = make_wav(1, 16000, 16, body)
                    if mode == "streaming-wav":
                        body = body[:4] + b"\xff" * 4 + body[8:40] + b"\xff" * 4 + body[44:]
                    content_type = "audio/wav"
            elif self.path == "/v1/tools/search" and (self.server.role == "search" or self.server.same_origin_search):
                assert isinstance(body["query"], str) and 1 <= len(body["query"]) <= 128
                assert body["count"] == 3 and body["need_content"] is False
                item = {"title": "Public documentation", "url": "https://example.test/docs", "summary": "Verified fixture result."}
                if mode == "bad-url":
                    item["url"] = "file:///private/not-a-web-result"
                if mode == "long-summary":
                    item["summary"] = "x" * 4000
                body = {"results": [] if mode == "empty" else [item]}
                if mode == "bad-results":
                    body["results"] = {"not": "an array"}
                content_type = "application/json"
            elif self.path == self.server.chat_path and self.server.role == "chat":
                assert body["model"] == "fixture-model" and body["max_tokens"] == 256 and body["stream"] is False
                assert "store" not in body and "max_completion_tokens" not in body
                assert not any(name.lower().startswith("x-hermes-") for name in self.headers)
                assert body.get("thinking") == {"type": "disabled"}
                messages = body["messages"]
                previous = [item for item in messages if item["role"] == "tool"]
                if "tools" in body and (not previous or mode in ("endless", "duplicate-id")):
                    assert [item["function"]["name"] for item in body["tools"]] == ["web_search"]
                    identifier = "search-0" if mode == "duplicate-id" else "search-" + str(len(previous))
                    arguments = json.dumps({"query": "café documentation"})
                    arguments = self.server.arguments if self.server.arguments is not None else arguments
                    call = {"id": identifier, "type": "function", "function": {"name": "web_search", "arguments": arguments}}
                    calls = [call]
                    if mode == "unknown-tool":
                        calls = [call, {"id": "forbidden", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]
                    if mode == "batch":
                        calls.append({**call, "id": "search-batch"})
                    body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}]}
                else:
                    if previous:
                        assert previous[-1]["tool_call_id"] in {call["id"] for item in messages if item.get("tool_calls") for call in item["tool_calls"]}
                        results = json.loads(previous[-1]["content"])["untrusted_results"]
                        assert not results or (results[0]["url"] == "https://example.test/docs" and len(results[0]["summary"]) <= 700)
                    body = {"choices": [{"message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}]}
                    if mode == "thinking":
                        body["choices"][0]["message"]["reasoning_content"] = "Unexpected thinking despite normal mode."
                content_type = "application/json"
            else:
                self.send_error(404)
                return
        self.server.calls.append(self.path)
        if mode == "slow":
            time.sleep(1)
        raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        if mode == "oversized":
            raw = b"x" * (1440045 if self.path == "/tts/bytes" else 65537)
        if mode == "invalid-json":
            raw = b"not valid JSON"
        self.send_response(503 if mode == "failure" else 307 if mode == "redirect" else 200)
        self.send_header("Content-Type", content_type)
        if mode == "encoded":
            self.send_header("Content-Encoding", "gzip")
        if mode == "redirect":
            self.send_header("Location", "https://not-an-approved-origin.test/")
        if mode != "oversized":
            self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass


def start_provider(home, secrets, role, cleanup):
    """TLS files are generated by the caller in its disposable directory, never discovered."""
    key_file = home / (role + "-fixture.key")
    key = "fixture-" + role + "-not-a-real-key"
    private_write(key_file, key)
    api = ThreadingHTTPServer(("127.0.0.1", 0), ProviderAPI)
    api.daemon_threads = True
    api.key, api.role, api.calls, api.rejected = key, role, [], 0
    api.mode, api.arguments, api.last_text = "ok", None, None
    api.chat_path, api.same_origin_search = "/v1/chat/completions", False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(secrets / "localhost.crt", secrets / "localhost.key")
    api.socket = context.wrap_socket(api.socket, server_side=True)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    cleanup.callback(thread.join, 2)
    cleanup.callback(api.server_close)
    cleanup.callback(api.shutdown)
    api.origin, api.key_file = f"https://localhost:{api.server_port}", str(key_file)
    return api
