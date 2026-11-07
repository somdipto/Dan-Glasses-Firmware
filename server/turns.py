"""Explicit finite pipeline or live Cartesia transport configuration."""
from __future__ import annotations

import os

try:
    from .backends import UpstreamError, configured_backends
except ImportError:
    from backends import UpstreamError, configured_backends


class SpeechReplyTurn:
    def __init__(self, speech, reply):
        self.speech = speech
        self.reply = reply
        self.speech_mode = getattr(speech, "mode", "injected")
        self.reply_mode = getattr(reply, "mode", "injected")

    def turn(self, wav, history, scope, remaining, authorize):
        heard = self.speech.transcribe(wav, remaining())
        remaining()
        authorize()
        if not isinstance(heard, str) or len(heard) > 2048:
            raise UpstreamError()
        heard = heard.strip()
        if not heard:
            return None
        reply = self.reply.chat(history, heard, scope, remaining())
        remaining()
        authorize()
        if not isinstance(reply, str) or not reply.strip() or len(reply) > 2048:
            raise UpstreamError()
        reply = reply.strip()
        return heard, reply, self.speech.synthesize(reply, remaining())


def configured_backend():
    mode = os.environ.get("VOICE_BACKEND", "pipeline")
    if mode == "pipeline":
        return SpeechReplyTurn(*configured_backends())
    if mode != "cartesia-stream":
        raise ValueError("Unknown voice backend")
    if any(name in os.environ for name in ("SPEECH_BACKEND", "REPLY_BACKEND", "HERMES_BACKEND")):
        raise ValueError("Managed agents cannot be combined with pipeline provider settings")
    if os.environ.get("WEB_SEARCH_BACKEND", "disabled") != "disabled":
        raise ValueError("Managed agents do not support a separate web search backend")
    try:
        from .cartesia_agent import CartesiaAgent
    except ImportError:
        from cartesia_agent import CartesiaAgent
    return CartesiaAgent(os.environ["CARTESIA_BASE_URL"], os.environ["CARTESIA_KEY_FILE"],
                         os.environ["CARTESIA_AGENT_ID"], os.environ["CARTESIA_VERSION"],
                         ca_file=os.environ.get("CARTESIA_CA_FILE"),
                         agent_version_id=os.environ["CARTESIA_AGENT_VERSION_ID"])
