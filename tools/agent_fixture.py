"""Disposable WSS Managed Agent fixture; synthetic PCM, no account or paid API.

Response boundaries are driven by received chunks, NEVER by quiet-time polling.
The replacement scenario cannot progress until the consumer sends later input.
"""
import base64
import json
import logging
import ssl
import threading
from urllib.parse import parse_qs, urlsplit

from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from tools.credentials import private_write

TRANSCRIPT = "What is the capital of France?"
REPLY = "The capital of France is Paris."
AGENT_ID = "agent_fixture"
VERSION_ID = "av_fixture"


class AgentFixture:
    def __init__(self, home, secrets, cleanup):
        self.key = "fixture-agent-not-a-real-key"
        key_file = home / "agent-fixture.key"
        private_write(key_file, self.key)
        self.key_file = str(key_file)
        self.mode, self.transcript, self.reply = "ok", TRANSCRIPT, REPLY
        self.redirect_origin = None
        self.calls, self.handshakes = [], []
        self.active, self.lock = set(), threading.Lock()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(secrets / "localhost.crt", secrets / "localhost.key")
        logger = logging.Logger("glasses-agent-fixture")
        logger.addHandler(logging.NullHandler())
        logger.propagate = False
        self.server = serve(self.handle, "127.0.0.1", 0, ssl=context, process_request=self.handshake,
                            compression=None, close_timeout=0.2, open_timeout=2, max_size=65536,
                            max_queue=4, logger=logger)
        self.origin = f"https://localhost:{self.server.socket.getsockname()[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        cleanup.callback(self.close)

    def close(self):
        self.server.shutdown()
        with self.lock:
            active = tuple(self.active)
        for connection in active:
            connection.close()
        self.thread.join(2)

    def handshake(self, connection, request):
        self.handshakes.append(request.path)
        parsed = urlsplit(request.path)
        if (request.headers.get("X-API-Key") != self.key
                or parsed.path != "/v1/agents/websocket/" + AGENT_ID
                or parse_qs(parsed.query) != {"cartesia_version": ["2026-08-14"]}):
            return connection.respond(401, "Unauthorized")
        if self.mode == "redirect":
            response = connection.respond(307, "Not followed")
            response.headers["Location"] = "wss" + (self.redirect_origin or self.origin)[5:] + "/redirect-target"
            return response
        return None

    def handle(self, connection):
        with self.lock:
            self.active.add(connection)
        record = None
        try:
            record = self.exchange(connection)
        except (ConnectionClosed, TimeoutError):
            pass
        finally:
            with self.lock:
                self.active.discard(connection)
            # Tests observe a real transport shutdown, not merely task bookkeeping.
            for call in self.calls:
                if call.get("connection") is connection:
                    call["closed"].set()

    def exchange(self, connection):
        mode, transcript, reply = self.mode, self.transcript, self.reply
        first = json.loads(connection.recv(timeout=2))
        assert first["type"] == "session_create"
        assert first["audio"] == {"input_format": "pcm_16000", "output_delivery": "as_available", "continuous_input": False}
        history = json.loads(first["dynamic_variables"]["conversation_history"])
        record = {"history": history, "input_bytes": 0, "closed": threading.Event(), "connection": connection,
                  "completed_at_input_bytes": []}
        self.calls.append(record)
        started = set()

        def send(event):
            if event["type"] == "turn_started":
                started.add(event["turn"])
            connection.send(json.dumps(event, ensure_ascii=False))

        def wait_closed():
            while True:
                incoming = json.loads(connection.recv(timeout=10))
                assert incoming["type"] == "audio_input", "No invented provider commit/flush event is permitted"
                chunk = base64.b64decode(incoming["audio"], validate=True)
                assert 0 < len(chunk) <= 3200 and len(chunk) % 2 == 0
                record["input_bytes"] += len(chunk)

        if mode == "hold-ready":
            wait_closed()
            return record
        if mode == "before-ready":
            send({"type": "audio_output", "audio": "AAA="})
            wait_closed()
            return record
        send({"type": "session_ready", "call_id": "ac_fixture", "agent_id": AGENT_ID,
              "agent_version_id": "av_changed" if mode == "version-drift" else VERSION_ID,
              "audio": {"input_format": "pcm_24000" if mode == "wrong-format" else "pcm_16000",
                        "output_delivery": "as_available"}})
        if mode in ("version-drift", "wrong-format", "timeout", "idle"):
            wait_closed()
            return record

        def receive_input():
            event = json.loads(connection.recv(timeout=5))
            assert event["type"] == "audio_input", "Only documented provider input events are permitted"
            chunk = base64.b64decode(event["audio"], validate=True)
            assert 0 < len(chunk) <= 3200 and len(chunk) % 2 == 0
            record["input_bytes"] += len(chunk)
            return chunk

        def ended(role, text, turn, interrupted=False):
            if turn not in started:
                send({"type": "turn_started", "turn": turn, "role": role, "start_time": 0.0})
            send({"type": "turn_ended", "turn": turn, "role": role, "text": text,
                  "interrupted": interrupted, "start_time": 0.0, "end_time": 0.5, "tool_calls": []})

        def audio(pcm):
            for offset in range(0, len(pcm), 32000):
                send({"type": "audio_output", "audio": base64.b64encode(pcm[offset:offset + 32000]).decode()})

        receive_input()
        if mode == "error":
            send({"type": "error", "code": "agent_failed", "fatal": True})
        elif mode == "unknown-tool":
            send({"type": "client_tool_call", "tool_name": "unconfigured_action"})
        elif mode == "binary":
            connection.send(b"not-json")
        elif mode == "malformed-json":
            connection.send("{")
        elif mode == "oversized-message":
            connection.send("x" * 65537)
        elif mode == "event-flood":
            for _ in range(4097):
                send({"type": "bounded_telemetry"})
        else:
            ended("user", "" if mode == "empty-transcript" else transcript, 1, mode == "interrupted-user")
            if mode in ("completed-during-input", "interrupt"):
                send({"type": "turn_started", "turn": 2, "role": "assistant"})
                audio(b"\x01\x00" * 8000)
                ended("assistant", "Wrong early answer.", 2, mode == "interrupt")
                record["completed_at_input_bytes"].append(record["input_bytes"])
                # Gate replacement on REAL later input, not upload replay or sleep.
                receive_input()
                send({"type": "audio_output_clear"})
                ended("user", "Please be concise.", 3)
            send({"type": "turn_started", "turn": 4, "role": "assistant"})
            send({"type": "turn_output_text_delta", "turn": 4, "role": "assistant", "text": "Not final."})
            size = {"minimum": 640, "maximum": 960000, "short-audio": 638,
                    "odd-audio": 15999, "oversized-audio": 960002}.get(mode, 16000)
            audio((b"\x80\x00" * ((size + 1) // 2))[:size])
            if mode != "partial":
                ended("assistant", reply, 4)
                record["completed_at_input_bytes"].append(record["input_bytes"])
        wait_closed()
        return record


def start_agent(home, secrets, cleanup):
    return AgentFixture(home, secrets, cleanup)
