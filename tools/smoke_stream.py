"""Exercise isolated native HTTPS/WSS with synthetic PCM; no live API or board."""
import asyncio
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp
from aiohttp import web

from server.cartesia_agent import CartesiaAgent
from server.server import Settings
from server.streaming import ACTIVE, STATE, create_stream_app
from tools.agent_fixture import AGENT_ID, VERSION_ID, TRANSCRIPT, REPLY, start_agent
from tools.credentials import private_replace
from tools.stream_client import PCMConsumer


async def exercise(home, secrets, api):
    registry = secrets / "devices.json"
    token = (secrets / "fixture.token").read_text().strip()
    ca = str(secrets / "localhost.crt")
    backend = CartesiaAgent(api.origin, api.key_file, AGENT_ID, "2026-08-14", ca, agent_version_id=VERSION_ID)
    app = create_stream_app(Settings(devices_file=registry, requests_per_minute=100, request_timeout=2,
                                    max_concurrent=1), backend)
    runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=1)
    await runner.setup()
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(ca, secrets / "localhost.key")
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    url = f"https://localhost:{site._server.sockets[0].getsockname()[1]}"
    headers = {"Authorization": "Bearer " + token, "X-Session": "stream-smoke"}
    context = ssl.create_default_context(cafile=ca)

    async def event(socket, consumer, target):
        async with asyncio.timeout(4):
            for _ in range(1024):
                kind = consumer.receive(await socket.receive())
                if kind == target:
                    return
                if kind == "error":
                    raise AssertionError("Unexpected stream failure")
        raise AssertionError("Missing bounded consumer event")

    async def no_io():
        async with asyncio.timeout(2):
            while app[ACTIVE] or api.active:
                await asyncio.sleep(0.01)
        assert all(call["closed"].is_set() for call in api.calls)
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()
                    and task.get_name().startswith("glasses-")
                    and task.get_name() != "glasses-session-reaper" and not task.done()]

    try:
        async with aiohttp.ClientSession(trust_env=False, connector=aiohttp.TCPConnector(ssl=context),
                                         timeout=aiohttp.ClientTimeout(total=5)) as client:
            async with client.get(url + "/health", headers=headers) as response:
                assert response.status == 200
                assert await response.json() == {"status": "ok", "speech": backend.speech_mode, "reply": backend.reply_mode}
            assert api.handshakes == [], "Health must not open a provider session"
            async with client.get(url + "/health") as response:
                assert response.status == 401
            try:
                await client.ws_connect(url + "/voice", headers=headers)
            except aiohttp.WSServerHandshakeError as error:
                assert error.status == 400
            else:
                raise AssertionError("Missing subprotocol was accepted")
            for target, ssl_value in ((url, ssl.create_default_context()), (url.replace("localhost", "127.0.0.1"), context)):
                try:
                    async with client.get(target + "/health", headers=headers, ssl=ssl_value):
                        raise AssertionError("Untrusted CA/hostname was accepted")
                except aiohttp.ClientConnectorCertificateError:
                    pass
            api.mode = "completed-during-input"
            async with client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",), compress=0) as socket:
                consumer = PCMConsumer()
                assert socket.protocol == "glasses.v1"
                await event(socket, consumer, "ready")
                await socket.send_bytes(b"\x00\x01" * 1600)
                await event(socket, consumer, "done")
                assert bytes(consumer.pcm) == b"\x01\x00" * 8000
                assert api.calls[-1]["input_bytes"] == 3200
                assert not app[STATE].sessions[("fixture", "stream-smoke")].messages
                await socket.send_bytes(b"\x00\x01" * 1600)
                await event(socket, consumer, "clear")
                assert not consumer.pcm and not consumer.done
                await event(socket, consumer, "done")
                assert bytes(consumer.pcm) == b"\x80\x00" * 8000
                # Bounded host sink clock; this is NOT audible/I2S evidence.
                while consumer.pcm:
                    consumer.drain(640)
                    await asyncio.sleep(0.02)
                await socket.send_str(consumer.played())
                await event(socket, consumer, "complete")
                assert list(app[STATE].sessions[("fixture", "stream-smoke")].messages) == [
                    {"role": "user", "content": TRANSCRIPT + " Please be concise."},
                    {"role": "assistant", "content": REPLY}]
            await no_io()
            print("Verified real WSS: pinned ready, early PCM BEFORE later input, immediate clear flush, replacement PCM, drained acknowledgement/history.")
            seed = list(app[STATE].sessions[("fixture", "stream-smoke")].messages)
            async with client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",)) as old:
                abandoned = PCMConsumer()
                await event(old, abandoned, "ready")
                await old.send_bytes(b"\x00\x01" * 1600)
                await event(old, abandoned, "done")
                assert bytes(abandoned.pcm) == b"\x01\x00" * 8000
                await old.send_str(abandoned.cancel())
                await event(old, abandoned, "cancelled")
                assert not abandoned.pcm and not abandoned.acknowledged
                # The wire acknowledgement alone permits same-session replacement.
                # Do not wait for old CLOSE, provider flags, no_io or a retry.
                api.mode = "ok"
                async with client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",)) as replacement:
                    current = PCMConsumer()
                    await event(replacement, current, "ready")
                    assert api.calls[-1]["history"] == seed
                    await replacement.send_bytes(b"\x00\x02" * 1600)
                    await event(replacement, current, "done")
                    assert bytes(current.pcm) == b"\x80\x00" * 8000
                    while current.pcm:
                        current.drain(640)
                    await replacement.send_str(current.played())
                    await event(replacement, current, "complete")
                    assert list(app[STATE].sessions[("fixture", "stream-smoke")].messages) == seed + [
                        {"role": "user", "content": TRANSCRIPT},
                        {"role": "assistant", "content": REPLY}]
                assert (await old.receive()).type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            await no_io()
            print("Verified cancel/cancelled handoff: abandoned PCM discarded, immediate same-session replacement with one slot, committed history retained, no interrupted pair or stale audio.")
            for mode, expected in (("version-drift", "upstream"), ("timeout", "timeout"), ("idle", "silence")):
                api.mode = mode
                fault_headers = {**headers, "X-Session": mode}
                async with client.ws_connect(url + "/voice", headers=fault_headers, protocols=("glasses.v1",)) as socket:
                    consumer = PCMConsumer()
                    if mode != "version-drift":
                        await event(socket, consumer, "ready")
                        await socket.send_bytes(b"\0\0" * 320 if mode == "idle" else b"\x00\x01" * 320)
                        if mode == "idle":
                            await socket.send_str("stop_input")
                    await event(socket, consumer, "error")
                    assert consumer.error == expected
                    if mode == "version-drift":
                        assert not consumer.ready and api.calls[-1]["input_bytes"] == 0
                await no_io()
                assert ("fixture", mode) not in app[STATE].sessions
            api.mode = "hold-ready"
            socket = await client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",))
            async with asyncio.timeout(2):
                while not api.active:
                    await asyncio.sleep(0.01)
            await socket.close()
            await no_io()
            socket = await client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",))
            before_ready = PCMConsumer()
            await socket.send_str(before_ready.cancel())
            await event(socket, before_ready, "cancelled")
            await socket.close()
            await no_io()
            api.mode = "idle"
            async with client.ws_connect(url + "/voice", headers=headers, protocols=("glasses.v1",)) as socket:
                consumer = PCMConsumer()
                await event(socket, consumer, "ready")
                private_replace(registry, json.dumps({}))
                await event(socket, consumer, "error")
                assert consumer.error == "unauthorized"
            await no_io()
            assert app[STATE].sessions == {}
            print("Verified TLS/hostname/auth/protocol rejection, silence, immutable-version drift, stalled-provider deadline, pre-ready cancellation and revocation; no lingering stream tasks or provider I/O.")
    finally:
        await runner.cleanup()


def smoke():
    with tempfile.TemporaryDirectory(prefix="glasses-native-wss-") as directory, ExitStack() as cleanup:
        home = Path(directory)
        secrets = home / "tls"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "TMPDIR": str(home),
               "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        for command in (("local-tls",), ("create-device", "fixture")):
            subprocess.run([sys.executable, "tools/credentials.py", "--directory", str(secrets), *command],
                           cwd=ROOT, env=env, check=True, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=35)
        api = start_agent(home, secrets, cleanup)
        asyncio.run(exercise(home, secrets, api))
    print("Fixture stopped; private temporary data removed. Synthetic host sink only; no hardware, speakers or live provider contacted.")


if __name__ == "__main__":
    smoke()
