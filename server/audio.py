"""Shared, strict bridge WAV format; no provider or device-specific conversions."""
import struct

def make_wav(channels, rate, bits, pcm):
    align = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * align, align, bits)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def parse_wav(data, *, output=False):
    """Reject contradictory lengths and non-16k mono PCM16 at bridge boundaries."""
    if len(data) > (1440044 if output else 330000) or len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Invalid WAV")
    if struct.unpack_from("<I", data, 4)[0] != len(data) - 8:
        raise ValueError("Invalid RIFF length")
    pos, fmt, pcm, chunks = 12, None, None, 0
    while pos < len(data):
        chunks += 1
        if chunks > 16 or pos + 8 > len(data):
            raise ValueError("Invalid chunk list")
        name, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        start, end = pos + 8, pos + 8 + size
        if end + (size & 1) > len(data):
            raise ValueError("Truncated WAV chunk")
        if name == b"fmt ":
            if fmt is not None or pcm is not None or size not in (16, 18):
                raise ValueError("Invalid WAV format chunk")
            tag, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", data, start)
            if (tag, channels, rate, bits, align, byte_rate) != (1, 1, 16000, 16, 2, 32000):
                raise ValueError("Unsupported PCM format")
            if size == 18 and data[start + 16:start + 18] != b"\0\0":
                raise ValueError("Unsupported PCM extension")
            fmt = (channels, rate, bits)
        elif name == b"data":
            if fmt is None or pcm is not None or not size or size % 2:
                raise ValueError("Invalid PCM data")
            pcm = data[start:end]
        pos = end + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("Missing WAV chunks")
    seconds = len(pcm) / (fmt[1] * 2)
    if not ((0.02 if output else 0.3) <= seconds <= (30 if output else 10)):
        raise ValueError("Unsupported audio duration")
    return (*fmt, pcm)
