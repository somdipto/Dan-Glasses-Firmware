"""Pinned Cartesia live session: native cancellable I/O, never a finite WAV turn."""
from __future__ import annotations

import asyncio
import json
import re
import ssl
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import certifi

try:
    from .backends import UpstreamError, private_secret, provider_origin
except ImportError:
    from backends import UpstreamError, private_secret, provider_origin


class CartesiaAgent:
    speech_mode = "cartesia-managed-agent"
    reply_mode = "cartesia-managed-llm"
    streaming = True

    def __init__(self, origin, key_file, agent_id, version, ca_file=None, *, agent_version_id):
        self.origin = provider_origin(origin, "api.cartesia.ai")
        private_secret(key_file)
        self.key_file = str(Path(key_file).absolute())
        if (not re.fullmatch(r"agent_[A-Za-z0-9_-]{1,128}", agent_id)
                or not re.fullmatch(r"av_[A-Za-z0-9_-]{1,128}", agent_version_id)):
            raise ValueError("Set the created Cartesia agent and immutable version IDs")
        if version != "2026-08-14":
            raise ValueError("Unsupported Cartesia contract version")
        self.agent_id, self.agent_version_id, self.version = agent_id, agent_version_id, version
        self.ca_file = str(Path(ca_file).resolve(strict=True)) if ca_file else None

    @asynccontextmanager
    async def connect(self, history):
        context = ssl.create_default_context(cafile=self.ca_file or certifi.where())
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        url = ("wss" + self.origin[5:] + "/v1/agents/websocket/" + self.agent_id
               + "?cartesia_version=" + self.version)
        attempted = False

        async def one_handshake(request, handler):
            # aiohttp 3.14.5 ws_connect does NOT expose allow_redirects. Middleware
            # runs before its redirect loop and again before a persistent retry.
            # Never let the custom provider key reach even a same-origin redirect.
            nonlocal attempted
            if attempted or str(request.url) != url:
                raise UpstreamError()
            attempted = True
            response = await handler(request)
            if response.status != 101:
                response.close()
                raise UpstreamError()
            return response

        async with aiohttp.ClientSession(
                trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                connector=aiohttp.TCPConnector(ssl=context, limit=1, force_close=True),
                timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_connect=5, sock_read=30),
                middlewares=(one_handshake,)) as client:
            async with client.ws_connect(
                    url, headers={"X-API-Key": private_secret(self.key_file)},
                    proxy=None, compress=0, max_msg_size=65536,
                    timeout=aiohttp.ClientWSTimeout(ws_close=0.2)) as socket:
                await socket.send_json({
                    "type": "session_create",
                    "audio": {"input_format": "pcm_16000", "output_delivery": "as_available",
                              "continuous_input": False},
                    "dynamic_variables": {"conversation_history": json.dumps(history, ensure_ascii=False)}})
                # No input is exposed until immutable configuration is verified.
                async with asyncio.timeout(5):
                    for _ in range(32):
                        event = await receive_event(socket)
                        if event["type"] == "session_ready":
                            audio = event.get("audio")
                            if (event.get("agent_id") != self.agent_id
                                    or event.get("agent_version_id") != self.agent_version_id
                                    or not isinstance(audio, dict)
                                    or audio.get("input_format") != "pcm_16000"
                                    or audio.get("output_delivery") != "as_available"):
                                raise UpstreamError()
                            break
                        if event["type"] not in ("session_created", "ping"):
                            raise UpstreamError()
                    else:
                        raise UpstreamError()
                yield socket


async def receive_event(socket):
    message = await socket.receive()
    if message.type != aiohttp.WSMsgType.TEXT:
        raise UpstreamError()
    try:
        event = json.loads(message.data)
    except (ValueError, UnicodeError):
        raise UpstreamError() from None
    if (not isinstance(event, dict) or not isinstance(event.get("type"), str)
            or len(event["type"]) > 64
            or event["type"] in ("error", "client_tool_call", "dtmf_output")):
        raise UpstreamError()
    return event
