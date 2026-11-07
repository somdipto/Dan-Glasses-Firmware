"""Private-development voice bridge. No import-time secrets, network or file writes."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import ssl
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import HTTPException, RequestEntityTooLarge
from werkzeug.serving import WSGIRequestHandler, make_server

try:
    from .backends import MockReply, MockSpeech, UpstreamError, UpstreamTimeout
    from .audio import make_wav, parse_wav
    from .turns import SpeechReplyTurn, configured_backend
except ImportError:
    from backends import MockReply, MockSpeech, UpstreamError, UpstreamTimeout
    from audio import make_wav, parse_wav
    from turns import SpeechReplyTurn, configured_backend

IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z", re.ASCII)
TOKEN = re.compile(r"Bearer ([A-Za-z0-9_-]{43})\Z", re.ASCII)


@dataclass(frozen=True)
class Settings:
    devices_file: Path
    max_upload_bytes: int = 330000
    max_response_bytes: int = 1440044
    requests_per_minute: int = 6
    health_per_minute: int = 60
    max_concurrent: int = 2
    max_sessions: int = 64
    sessions_per_device: int = 8
    history_turns: int = 4
    idle_seconds: float = 300
    max_age_seconds: float = 3600
    request_timeout: float = 90
    silence_rms: int = 40

    def __post_init__(self):
        for name in ("max_upload_bytes", "max_response_bytes", "requests_per_minute", "health_per_minute",
                     "max_concurrent", "max_sessions", "sessions_per_device", "history_turns",
                     "idle_seconds", "max_age_seconds", "request_timeout", "silence_rms"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Limits must be positive finite numbers")
        if self.max_upload_bytes > 330000 or self.max_response_bytes > 1440044 or self.max_concurrent > 8 or self.request_timeout > 90:
            raise ValueError("Configured limit exceeds the supported bound")

    @classmethod
    def from_env(cls):
        return cls(devices_file=Path(os.environ["BRIDGE_DEVICES_FILE"]),
                   requests_per_minute=int(os.environ.get("BRIDGE_REQUESTS_PER_MINUTE", "6")),
                   request_timeout=float(os.environ.get("BRIDGE_REQUEST_TIMEOUT", "90")),
                   silence_rms=int(os.environ.get("BRIDGE_SILENCE_RMS", "40")))


def load_devices(path):
    if path.is_symlink() or not path.is_file() or (os.name == "posix" and path.stat().st_mode & 0o077):
        raise ValueError("Device registry must be private")
    with path.open("rb") as stream:
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError("Device registry exceeds its bound")
    devices = json.loads(raw)
    if not isinstance(devices, dict) or len(devices) > 128:
        raise ValueError("Invalid device registry")
    if any(not IDENTIFIER.fullmatch(k) or not isinstance(v, str) or
           not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in devices.items()):
        raise ValueError("Invalid device credential digest")
    return devices




@dataclass
class Conversation:
    created: float
    touched: float
    credential: str = ""
    scope: str = field(default_factory=lambda: secrets.token_hex(24))
    messages: deque = field(default_factory=deque)
    busy: bool = False


class BridgeState:
    def __init__(self, settings, clock):
        self.settings, self.clock = settings, clock
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(settings.max_concurrent)
        self.sessions = {}
        self.rates = {}

    def prune(self, devices):
        now = self.clock()
        for key, entry in list(self.sessions.items()):
            changed = entry.credential != devices.get(key[0])
            expired = now - entry.touched >= self.settings.idle_seconds or now - entry.created >= self.settings.max_age_seconds
            if changed or (expired and not entry.busy):
                del self.sessions[key]
        for key in list(self.rates):
            if key[0] not in devices:
                del self.rates[key]

    def allow(self, device, health):
        key = (device, health)
        now = self.clock()
        times = self.rates.setdefault(key, deque())
        while times and now - times[0] >= 60:
            times.popleft()
        maximum = self.settings.health_per_minute if health else self.settings.requests_per_minute
        if len(times) >= maximum:
            return False
        times.append(now)
        return True

def current_identity(settings, authorization):
    """One registry and constant-time token convention for HTTP and WSS."""
    devices = load_devices(settings.devices_file)
    match = TOKEN.fullmatch(authorization) if len(authorization) <= 512 else None
    digest = hashlib.sha256((match[1] if match else "").encode("ascii")).hexdigest()
    identity = None
    for device, expected in devices.items():
        if hmac.compare_digest(digest, expected) and match:
            identity = device
    return devices, identity


class _AuthorizationChanged(Exception):
    def __init__(self, code, status):
        self.code, self.status = code, status



def create_app(settings, backend=None, *, clock=time.monotonic):
    if getattr(backend, "streaming", False):
        raise ValueError("Live Cartesia requires create_stream_app, never finite /ask")
    backend = backend if backend is not None else SpeechReplyTurn(MockSpeech(), MockReply())
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=settings.max_upload_bytes, JSON_AS_ASCII=False)
    app.json.ensure_ascii = False
    state = BridgeState(settings, clock)
    app.extensions["voice_bridge"] = state

    def failure(code, status):
        response = jsonify(error=code)
        response.status_code = status
        if status in (429, 503):
            response.headers["Retry-After"] = "5"
        return response

    def request_identity():
        return current_identity(settings, request.headers.get("Authorization", ""))

    @app.before_request
    def authenticate():
        with state.lock:
            try:
                devices, identity = request_identity()
            except (OSError, ValueError, TypeError):
                return failure("credential_configuration_unavailable", 503)
            state.prune(devices)
            if identity is None:
                return failure("unauthorized", 401)
            g.device = identity
            g.digest = devices[identity]
            if not state.allow(identity, request.path == "/health"):
                return failure("rate_limited", 429)
        if request.path in ("/ask", "/reset"):
            g.session = request.headers.get("X-Session", "default")
            if not IDENTIFIER.fullmatch(g.session):
                return failure("invalid_session", 400)

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        return failure("audio_too_large", 413)

    @app.errorhandler(Exception)
    def unexpected(error):
        if isinstance(error, HTTPException):
            return failure("request_rejected", error.code)
        return failure("internal_error", 500)

    @app.get("/health")
    def health():
        return jsonify(status="ok", speech=backend.speech_mode, reply=backend.reply_mode)

    @app.post("/reset")
    def reset():
        key = (g.device, g.session)
        with state.lock:
            entry = state.sessions.get(key)
            if entry and entry.credential != g.digest:
                return failure("unauthorized", 401)
            if entry and entry.busy:
                return failure("session_busy", 409)
            state.sessions.pop(key, None)
        return jsonify(status="cleared")

    @app.post("/ask")
    def ask():
        raw_length = request.environ.get("CONTENT_LENGTH", "")
        if not raw_length:
            return failure("content_length_required", 411)
        if not re.fullmatch(r"[0-9]{1,9}", raw_length):
            return failure("invalid_content_length", 400)
        if int(raw_length) > settings.max_upload_bytes:
            return failure("audio_too_large", 413)
        if request.mimetype != "audio/wav":
            return failure("audio_wav_required", 415)
        if not state.slots.acquire(blocking=False):
            return failure("bridge_busy", 503)
        entry = None
        created = False
        committed = False
        deadline = clock() + settings.request_timeout
        key = (g.device, g.session)

        def authorize():
            try:
                devices, identity = request_identity()
            except (OSError, ValueError, TypeError):
                raise _AuthorizationChanged("credential_configuration_unavailable", 503) from None
            with state.lock:
                if (identity != g.device or not hmac.compare_digest(devices.get(g.device, ""), g.digest)
                        or (entry is not None and state.sessions.get(key) is not entry)):
                    if entry is not None and state.sessions.get(key) is entry:
                        state.sessions.pop(key, None)
                    raise _AuthorizationChanged("unauthorized", 401)

        def remaining():
            seconds = deadline - clock()
            if seconds <= 0:
                raise UpstreamTimeout()
            return seconds

        try:
            data = request.get_data(cache=False)
            try:
                channels, rate, bits, pcm = parse_wav(data)
            except (ValueError, struct.error):
                return failure("invalid_audio", 422)
            if sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", pcm)) <= (len(pcm) // 2) * settings.silence_rms ** 2:
                return Response(status=204)
            remaining()
            authorize()
            with state.lock:
                entry = state.sessions.get(key)
                if entry and entry.credential != g.digest:
                    entry = None
                    raise _AuthorizationChanged("unauthorized", 401)
                if entry and entry.busy:
                    entry = None
                    return failure("session_busy", 409)
                if entry is None:
                    count = sum(k[0] == g.device for k in state.sessions)
                    if count >= settings.sessions_per_device or len(state.sessions) >= settings.max_sessions:
                        return failure("session_limit", 429)
                    entry = Conversation(created=clock(), touched=clock(), credential=g.digest,
                                         messages=deque(maxlen=settings.history_turns * 2))
                    state.sessions[key] = entry
                    created = True
                entry.busy = True
                history = list(entry.messages)
            remaining()
            authorize()
            result = backend.turn(make_wav(channels, rate, bits, pcm), history, entry.scope,
                                  remaining, authorize)
            remaining()
            authorize()
            if result is None:
                return Response(status=204)
            if not isinstance(result, tuple) or len(result) != 3:
                raise UpstreamError()
            heard, reply, wav = result
            if any(not isinstance(text, str) or not text.strip() or len(text) > 2048
                   for text in (heard, reply)):
                raise UpstreamError()
            heard, reply = heard.strip(), reply.strip()
            remaining()
            if not isinstance(wav, bytes) or len(wav) > settings.max_response_bytes:
                raise UpstreamError()
            try:
                channels, rate, bits, pcm = parse_wav(wav, output=True)
            except (ValueError, struct.error):
                raise UpstreamError() from None
            authorize()
            remaining()
            with state.lock:
                if state.sessions.get(key) is not entry:
                    raise _AuthorizationChanged("unauthorized", 401)
                entry.messages.extend(({"role": "user", "content": heard}, {"role": "assistant", "content": reply}))
                entry.touched = clock()
                committed = True
            return Response(make_wav(channels, rate, bits, pcm), content_type="audio/wav")
        except _AuthorizationChanged as error:
            return failure(error.code, error.status)
        except UpstreamTimeout:
            return failure("upstream_timeout", 504)
        except UpstreamError:
            return failure("upstream_unavailable", 502)
        finally:
            if entry:
                with state.lock:
                    entry.busy = False
                    if created and not committed and state.sessions.get(key) is entry:
                        state.sessions.pop(key, None)
            state.slots.release()

    return app


class PrivateRequestHandler(WSGIRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()
        self.expiry = threading.Timer(110, self.expire)
        self.expiry.daemon = True
        self.expiry.start()

    def expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        try:
            super().finish()
        finally:
            self.expiry.cancel()

    def log(self, _type, _message, *args):
        pass


def main():
    try:
        settings = Settings.from_env()
        load_devices(settings.devices_file)
        cert, key = os.environ["BRIDGE_TLS_CERT"], os.environ["BRIDGE_TLS_KEY"]
        if os.name == "posix" and Path(key).stat().st_mode & 0o077:
            raise ValueError("TLS key must be private")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        backend = configured_backend()
        if getattr(backend, "streaming", False):
            try:
                from .streaming import create_stream_app
            except ImportError:
                from streaming import create_stream_app
            app = create_stream_app(settings, backend)
        else:
            app = create_app(settings, backend)
        port = int(os.environ.get("BRIDGE_PORT", "8443"))
        if not 1024 <= port <= 65535:
            raise ValueError("Invalid local port")
    except (KeyError, OSError, ValueError):
        raise SystemExit("Bridge configuration invalid. Set explicit private credential and TLS files. No secrets printed.") from None
    if getattr(backend, "streaming", False):
        from aiohttp import web
        print(f"Voice bridge ready on loopback port {port}; speech={backend.speech_mode}; reply={backend.reply_mode}", flush=True)
        web.run_app(app, host="127.0.0.1", port=port, ssl_context=context,
                    access_log=None, print=None, handler_cancellation=True,
                    shutdown_timeout=2)
        return
    # ponytail: one process owns rate and session state. Do not multiply WSGI workers.
    server = make_server("127.0.0.1", port, app, threaded=False,
                         ssl_context=context, request_handler=PrivateRequestHandler)
    server.socket.do_handshake_on_connect = False
    stop = threading.Event()
    state = app.extensions["voice_bridge"]

    def expire_sessions():
        while not stop.wait(5):
            with state.lock:
                try:
                    devices = load_devices(settings.devices_file)
                except (OSError, ValueError, TypeError):
                    devices = {}
                state.prune(devices)

    reaper = threading.Thread(target=expire_sessions, daemon=True)
    reaper.start()
    print(f"Voice bridge ready on loopback port {port}; speech={backend.speech_mode}; reply={backend.reply_mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        reaper.join(2)
        server.server_close()


if __name__ == "__main__":
    main()
