"""
Test the laptop server without the ESP32.

1. Start the server:   python server/server.py
2. In another window:  python tools/test_server.py [question.wav]

Without a WAV argument it makes a spoken question with Windows' built-in
voice (Windows only), sends it to /ask and saves the reply as reply.wav.
"""

import subprocess
import struct
import sys
import tempfile
import time
from pathlib import Path

import requests

SERVER = "http://127.0.0.1:5000"


def make_question(path):
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{path}', $f);"
        "$s.Speak('What is the tallest mountain in the world?');$s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)


def silence_wav(seconds=1.5, rate=16000):
    pcm = b"\x00\x00" * int(seconds * rate)
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def main():
    try:
        print("health:", requests.get(f"{SERVER}/health", timeout=5).json())
    except requests.RequestException:
        sys.exit("Server is not running. Start it with: python server/server.py")

    if len(sys.argv) > 1:
        question = Path(sys.argv[1]).read_bytes()
    else:
        tmp = Path(tempfile.gettempdir()) / "va_question.wav"
        make_question(tmp)
        question = tmp.read_bytes()

    started = time.time()
    r = requests.post(f"{SERVER}/ask", data=question, timeout=120,
                      headers={"Content-Type": "audio/wav", "X-Device": "test"})
    print(f"/ask -> {r.status_code} in {time.time() - started:.1f}s")
    if r.status_code != 200:
        sys.exit(f"FAILED: {r.text[:300]}")
    print("  heard:", r.headers.get("X-Transcript"))
    print("  reply:", r.headers.get("X-Reply"))
    print("  audio:", len(r.content), "bytes", r.headers.get("Content-Type"))
    Path("reply.wav").write_bytes(r.content)
    print("  saved reply.wav - play it to hear the assistant")

    r = requests.post(f"{SERVER}/ask", data=silence_wav(), timeout=60,
                      headers={"Content-Type": "audio/wav", "X-Device": "test"})
    print(f"silence -> {r.status_code} (204 = correctly ignored)")
    requests.post(f"{SERVER}/reset", timeout=5)
    print("PASS" if r.status_code == 204 else "CHECK: silence was not ignored")


if __name__ == "__main__":
    main()
