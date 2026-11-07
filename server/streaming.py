"""Authenticated live PCM bridge. Every spawned task is cancelled and joined.

There is no whole-utterance buffer or playback selector. aiohttp's finite
WebSocketDataQueue pauses its socket reader; awaited writes apply backpressure.
Only one provider event is held by the application, and PCM is released live.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import re
import struct
import time
from collections import deque
from contextlib import suppress

import aiohttp
from aiohttp import web

try:
    from .backends import UpstreamError
    from .cartesia_agent import receive_event
    from .server import BridgeState, Conversation, IDENTIFIER, current_identity, load_devices
except ImportError:
    from backends import UpstreamError
    from cartesia_agent import receive_event
    from server import BridgeState, Conversation, IDENTIFIER, current_identity, load_devices

STATE = web.AppKey("voice_bridge", BridgeState)
ACTIVE = web.AppKey("voice_tasks", set)
PLAYED = re.compile(r"played ([1-9][0-9]{0,9})\Z", re.ASCII)


class StreamError(Exception):
    def __init__(self, code):
        self.code = code


def failure(code, status):
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if status in (429, 503):
        headers["Retry-After"] = "5"
    return web.json_response({"error": code}, status=status, headers=headers)


class LiveTurn:
    def __init__(self, socket, upstream, authorize, settings):
        self.socket, self.upstream = socket, upstream
        self.authorize, self.settings = authorize, settings
        self.revision = 0
        self.speaking_active = False
        self.pending = None
        self.heard = ""
        self.audio_bytes = self.generated = self.received = self.energy = 0
        self.input_stopped = False
        self.events = self.inputs = 0
        self.turns = {}
        self.seen_turns = set()
        self.assistant_turn = None

    async def send(self, text):
        self.authorize()
        await self.socket.send_str(text)

    async def clear(self):
        if self.revision:
            self.pending = None
            self.audio_bytes = 0
            self.speaking_active = False
            self.assistant_turn = None
            await self.send(f"clear {self.revision}")

    async def speaking(self):
        if self.speaking_active:
            await self.clear()
        self.revision += 1
        if self.revision > 128:
            raise StreamError("upstream")
        self.speaking_active = True
        await self.send(f"speaking {self.revision}")

    async def input(self):
        # Exactly one receiver owns the device socket. Provider output runs
        # concurrently, including while this receiver forwards later input.
        async for message in self.socket:
            self.authorize()
            if message.type == aiohttp.WSMsgType.TEXT and message.data == "cancel":
                # Cancellation is valid even while provider connect is pending.
                # None is not a successful played acknowledgement.
                return None
            if self.upstream is None:
                raise StreamError("protocol")
            self.inputs += 1
            if self.inputs > 1024:
                raise StreamError("protocol")
            if message.type == aiohttp.WSMsgType.BINARY:
                pcm = message.data
                if self.input_stopped or not pcm or len(pcm) > 3200 or len(pcm) % 2:
                    raise StreamError("invalid_audio")
                self.received += len(pcm)
                if self.received > min(320000, self.settings.max_upload_bytes):
                    raise StreamError("invalid_audio")
                self.energy += sum(sample[0] ** 2 for sample in struct.iter_unpack("<h", pcm))
                self.authorize()
                await self.upstream.send_json({"type": "audio_input", "audio": base64.b64encode(pcm).decode("ascii")})
            elif message.type == aiohttp.WSMsgType.TEXT:
                text = message.data
                if len(text) > 64 or not text.isascii():
                    raise StreamError("protocol")
                if text == "stop_input" and not self.input_stopped:
                    self.input_stopped = True
                    if not self.received or self.energy <= (self.received // 2) * self.settings.silence_rms ** 2:
                        raise StreamError("silence")
                    # This is ONLY a bridge control. Cartesia has no documented
                    # commit/input_done message; its VAD controls turn boundaries.
                    continue
                match = PLAYED.fullmatch(text)
                if match and int(match[1]) <= 2147483647:
                    revision = int(match[1])
                    if self.pending is None or revision != self.pending[0]:
                        raise StreamError("protocol")
                    if not self.received or self.energy <= (self.received // 2) * self.settings.silence_rms ** 2:
                        raise StreamError("silence")
                    return self.pending
                raise StreamError("protocol")
            else:
                raise StreamError("protocol")
        raise StreamError("cancelled")

    async def output(self):
        while True:
            event = await receive_event(self.upstream)
            self.events += 1
            if self.events > 4096:
                raise StreamError("upstream")
            kind = event["type"]
            if kind == "audio_output_clear":
                await self.clear()
            elif kind == "turn_started":
                index, role = event.get("turn"), event.get("role")
                if (type(index) is not int or not 0 <= index <= 2147483647
                        or index in self.seen_turns or len(self.seen_turns) >= 128
                        or role not in ("user", "assistant")):
                    raise StreamError("upstream")
                self.seen_turns.add(index)
                if role == "assistant":
                    if self.assistant_turn is not None or not self.speaking_active or self.pending is not None:
                        await self.speaking()
                    self.assistant_turn = index
                self.turns[index] = (role, self.revision if role == "assistant" else None)
            elif kind == "audio_output":
                if self.pending is not None:
                    raise StreamError("upstream")
                try:
                    encoded = event["audio"]
                    if not isinstance(encoded, str) or len(encoded) > 44000:
                        raise ValueError()
                    pcm = base64.b64decode(encoded, validate=True)
                except (KeyError, ValueError, binascii.Error):
                    raise StreamError("upstream") from None
                if not pcm or len(pcm) % 2 or len(pcm) > 32000:
                    raise StreamError("upstream")
                self.generated += len(pcm)
                self.audio_bytes += len(pcm)
                if self.generated > 960000:
                    raise StreamError("upstream")
                if not self.speaking_active:
                    await self.speaking()
                for offset in range(0, len(pcm), 3200):
                    self.authorize()
                    await self.socket.send_bytes(pcm[offset:offset + 3200])
            elif kind == "turn_ended":
                role, text = event.get("role"), event.get("text")
                if (role not in ("user", "assistant") or not isinstance(text, str)
                        or len(text) > 2048 or event.get("tool_calls")):
                    raise StreamError("upstream")
                index = event.get("turn")
                if type(index) is not int or index not in self.turns:
                    raise StreamError("upstream")
                started_role, revision = self.turns.pop(index)
                if role != started_role:
                    raise StreamError("upstream")
                if role == "assistant" and (revision != self.revision or not self.speaking_active):
                    # An already-cleared/replaced provider turn can never become
                    # the final text for the current playback revision.
                    continue
                if event.get("interrupted") is not False:
                    if role == "assistant" and event.get("interrupted") is True:
                        await self.clear()
                        continue
                    raise StreamError("upstream")
                text = text.strip()
                if role == "user":
                    self.heard = (self.heard + " " + text).strip()
                    if not text or len(self.heard) > 2048:
                        raise StreamError("upstream")
                else:
                    if (not self.heard or not text or self.audio_bytes < 640
                            or self.pending is not None or not self.revision):
                        raise StreamError("upstream")
                    self.pending = (self.revision, self.heard, text)
                    await self.send(f"done {self.revision}")
            elif kind not in ("turn_output_text_delta", "turn_input_text_delta", "ping"):
                # Unknown telemetry is bounded by message/event/deadline limits;
                # known action/error events are rejected by receive_event.
                if kind == "session_ready":
                    raise StreamError("upstream")


async def exchange(socket, backend, history, authorize, settings):
    tasks = []
    try:
        async with asyncio.timeout(min(60, settings.request_timeout)):
            # Revocation is checked even when provider connect/receive/send or
            # downstream backpressure stalls. No blocking provider worker exists.
            async def watch():
                while True:
                    authorize()
                    await asyncio.sleep(0.05)

            turn = LiveTurn(socket, None, authorize, settings)

            async def conversation():
                async with backend.connect(history) as upstream:
                    authorize()
                    await socket.send_str("ready")
                    turn.upstream = upstream
                    await turn.output()

            guard = asyncio.create_task(watch(), name="glasses-revocation")
            receiver = asyncio.create_task(turn.input(), name="glasses-device-input")
            work = asyncio.create_task(conversation(), name="glasses-provider-output")
            tasks.extend((guard, receiver, work))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if guard in done:
                guard.result()
            if work in done:
                work.result()
                raise StreamError("upstream")
            result = receiver.result()
            # A clear already being processed invalidates this acknowledgement.
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            if result is not None and turn.pending != result:
                raise StreamError("protocol")
            authorize()
            return result
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_stream_app(settings, backend, *, clock=time.monotonic):
    """Reusable native app; caller owns approved bind/TLS. No import-time I/O.

    The CLI binds loopback only. A separate isolated fixture may supply its own
    TCPSite address; this does not introduce a second credential/session store.
    """
    if not getattr(backend, "streaming", False):
        raise ValueError("Streaming application requires explicit cartesia-stream backend")
    state = BridgeState(settings, clock)

    @web.middleware
    async def authenticate(request, handler):
        if not request.secure:
            return failure("https_required", 400)
        # No browser cross-origin interface or query credentials are accepted.
        if request.query_string or "Origin" in request.headers:
            return failure("request_rejected", 400)
        try:
            devices, identity = current_identity(settings, request.headers.get("Authorization", ""))
        except (OSError, ValueError, TypeError):
            return failure("credential_configuration_unavailable", 503)
        with state.lock:
            state.prune(devices)
            if identity is None:
                return failure("unauthorized", 401)
            if not state.allow(identity, request.path == "/health"):
                return failure("rate_limited", 429)
        request["device"], request["digest"] = identity, devices[identity]
        if request.path in ("/voice", "/reset"):
            session = request.headers.get("X-Session", "")
            if not IDENTIFIER.fullmatch(session):
                return failure("invalid_session", 400)
            request["session"] = session
        try:
            response = await handler(request)
        except web.HTTPException as error:
            return failure("request_rejected", error.status)
        if not response.prepared:
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return response

    app = web.Application(middlewares=[authenticate], client_max_size=3200)
    app[STATE], app[ACTIVE] = state, set()

    async def health(_request):
        return web.json_response({"status": "ok", "speech": backend.speech_mode, "reply": backend.reply_mode})

    async def reset(request):
        key = (request["device"], request["session"])
        with state.lock:
            entry = state.sessions.get(key)
            if entry and entry.credential != request["digest"]:
                return failure("unauthorized", 401)
            if entry and entry.busy:
                return failure("session_busy", 409)
            state.sessions.pop(key, None)
        return web.json_response({"status": "cleared"})

    async def voice(request):
        if request.headers.get("Sec-WebSocket-Protocol", "") != "glasses.v1":
            return failure("subprotocol_required", 400)
        socket = web.WebSocketResponse(protocols=("glasses.v1",), compress=False,
                                       max_msg_size=6400, writer_limit=6400,
                                       timeout=0.2, autoping=True, autoclose=False)
        # Acknowledge peer CLOSE only in finally, after provider tasks have
        # joined and session/concurrency ownership is released for barge-in.
        if not socket.can_prepare(request).ok:
            return failure("websocket_required", 400)
        if not state.slots.acquire(blocking=False):
            return failure("bridge_busy", 503)
        entry, created, committed, cancelled = None, False, False, False
        completion = None
        key, digest = (request["device"], request["session"]), request["digest"]
        owner = asyncio.current_task()
        app[ACTIVE].add(owner)

        def authorize():
            try:
                devices, identity = current_identity(settings, request.headers.get("Authorization", ""))
            except (OSError, ValueError, TypeError):
                raise StreamError("unauthorized") from None
            with state.lock:
                state.prune(devices)
                if (identity != key[0] or not hmac.compare_digest(devices.get(key[0], ""), digest)
                        or state.sessions.get(key) is not entry):
                    if state.sessions.get(key) is entry:
                        state.sessions.pop(key, None)
                    raise StreamError("unauthorized")
                if clock() - entry.created >= settings.max_age_seconds:
                    state.sessions.pop(key, None)
                    raise StreamError("unauthorized")

        try:
            with state.lock:
                existing = state.sessions.get(key)
                if existing and existing.credential != digest:
                    return failure("unauthorized", 401)
                if existing and existing.busy:
                    return failure("session_busy", 409)
                if existing is None:
                    if (sum(k[0] == key[0] for k in state.sessions) >= settings.sessions_per_device
                            or len(state.sessions) >= settings.max_sessions):
                        return failure("session_limit", 429)
                    existing = Conversation(created=clock(), touched=clock(), credential=digest,
                                            messages=deque(maxlen=settings.history_turns * 2))
                    state.sessions[key] = existing
                    created = True
                entry = existing
                entry.busy = True
                history = list(entry.messages)
            authorize()
            socket.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
            await socket.prepare(request)
            async with asyncio.timeout(min(60, settings.request_timeout)):
                result = await exchange(socket, backend, history, authorize, settings)
                authorize()
                if result is None:
                    cancelled = True
                else:
                    revision, heard, reply = result
                    with state.lock:
                        entry.messages.extend(({"role": "user", "content": heard}, {"role": "assistant", "content": reply}))
                        entry.touched = clock()
                        committed = True
                    completion = revision
        except asyncio.CancelledError:
            cancelled = False
            completion = None
            raise
        except (StreamError, TimeoutError, UpstreamError, aiohttp.ClientError, OSError, ValueError) as error:
            cancelled = False
            completion = None
            code = error.code if isinstance(error, StreamError) else "timeout" if isinstance(error, TimeoutError) else "upstream"
            if not socket.prepared:
                return failure("unauthorized" if code == "unauthorized" else "upstream_unavailable",
                               401 if code == "unauthorized" else 502)
            if socket.prepared and not socket.closed:
                # error is itself a mandatory playback flush on the consumer.
                with suppress(TimeoutError, OSError, aiohttp.ClientError):
                    async with asyncio.timeout(0.25):
                        await socket.send_str("error " + code)
        finally:
            if entry is not None:
                with state.lock:
                    entry.busy = False
                    if created and not committed and state.sessions.get(key) is entry:
                        state.sessions.pop(key, None)
            state.slots.release()
            app[ACTIVE].discard(owner)
            if socket.prepared:
                with suppress(TimeoutError, OSError, aiohttp.ClientError):
                    async with asyncio.timeout(0.25):
                        if cancelled:
                            # All exchange tasks are joined and ownership is
                            # released before this explicit reconnect barrier.
                            await socket.send_str("cancelled")
                        elif completion is not None:
                            await socket.send_str(f"complete {completion}")
                        await socket.close()
        return socket

    async def reaper():
        while True:
            await asyncio.sleep(1)
            with state.lock:
                try:
                    devices = load_devices(settings.devices_file)
                except (OSError, ValueError, TypeError):
                    devices = {}
                state.prune(devices)

    async def lifecycle(_app):
        task = asyncio.create_task(reaper(), name="glasses-session-reaper")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(_app):
        tasks = tuple(app[ACTIVE])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    app.add_routes([web.get("/health", health), web.post("/reset", reset), web.get("/voice", voice)])
    return app
