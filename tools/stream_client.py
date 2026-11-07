"""Bounded glasses.v1 host consumer for isolated protocol smoke/tests.

`drain` represents bytes handed to a consumer-owned sink, not actual I2S proof.
A device must instead acknowledge only after its physical playback drains.
"""
import re

from aiohttp import WSMsgType

CONTROL = re.compile(r"(speaking|clear|done|complete) ([1-9][0-9]{0,9})\Z", re.ASCII)
ERRORS = {"unauthorized", "invalid_audio", "protocol", "silence", "timeout", "upstream", "busy", "cancelled"}


class PCMConsumer:
    def __init__(self):
        self.ready = self.done = self.complete = False
        self.revision = self.acknowledged = self.clears = self.total = 0
        self.pcm = bytearray()
        self.error = None
        self.cancelling = self.cancelled = False

    def receive(self, message):
        if self.cancelled:
            raise ValueError("Cancelled stream is terminal")
        if message.type == WSMsgType.BINARY:
            pcm = message.data
            if not self.ready or not self.revision or self.done or not pcm or len(pcm) > 3200 or len(pcm) % 2:
                raise ValueError("Invalid output PCM")
            self.total += len(pcm)
            if self.total > 960000:
                raise ValueError("Output exceeds stream bound")
            if not self.cancelling:
                self.pcm.extend(pcm)
            return "discarded" if self.cancelling else "audio"
        if message.type != WSMsgType.TEXT:
            raise ValueError("Unexpected socket close")
        text = message.data
        if len(text) > 64 or not text.isascii():
            raise ValueError("Invalid control")
        if text == "cancelled":
            if not self.cancelling:
                raise ValueError("Unsolicited cancellation acknowledgement")
            self.pcm.clear()
            self.done = self.cancelling = False
            self.cancelled = True
            return "cancelled"
        if text == "ready" and not self.ready:
            self.ready = True
            return "ready"
        if text.startswith("error ") and text[6:] in ERRORS:
            self.pcm.clear()
            self.done = False
            self.error = text[6:]
            return "error"
        match = CONTROL.fullmatch(text)
        if not self.ready or not match or int(match[2]) > 2147483647:
            raise ValueError("Invalid control")
        kind, revision = match[1], int(match[2])
        if kind == "speaking":
            if revision <= self.revision:
                raise ValueError("Stale speaking revision")
            self.revision, self.done = revision, False
            self.pcm.clear()
        elif revision != self.revision:
            raise ValueError("Mismatched revision")
        elif kind == "clear":
            self.pcm.clear()
            self.done = False
            self.clears += 1
        elif kind == "done":
            if self.done:
                raise ValueError("Duplicate final")
            self.done = True
        elif kind == "complete":
            if revision != self.acknowledged or not self.done or self.pcm:
                raise ValueError("Completion before acknowledged drain")
            self.complete = True
        return kind

    def drain(self, maximum=3200):
        if maximum <= 0 or maximum % 2:
            raise ValueError("Sink drain must align samples")
        pcm = bytes(self.pcm[:maximum])
        del self.pcm[:len(pcm)]
        return pcm

    def played(self):
        if (not self.done or self.pcm or self.error or self.acknowledged
                or self.cancelling or self.cancelled):
            raise ValueError("Cannot acknowledge undrained or invalidated audio")
        self.acknowledged = self.revision
        return f"played {self.revision}"

    def cancel(self):
        if self.cancelling or self.cancelled or self.complete or self.error or self.acknowledged:
            raise ValueError("Cannot cancel a completed, failed or already cancelling exchange")
        self.pcm.clear()
        self.cancelling = True
        return "cancel"
