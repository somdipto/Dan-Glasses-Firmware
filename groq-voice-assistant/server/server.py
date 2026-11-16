"""
Voice assistant server (runs on the laptop).

ESP32 --(WAV)--> /ask --> Groq STT --> Groq chat --> Groq TTS --(WAV)--> ESP32

Also answers UDP discovery broadcasts so the ESP32 finds this laptop
automatically, whatever its IP address is.

Run:  python server/server.py
"""

import os
import re
import socket
import struct
import sys
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request

# Windows consoles/redirects may not be UTF-8; never crash on printing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"


class Tee:
    """Print to the console and also append to server/server.log."""

    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def __getattr__(self, name):  # isatty, encoding, ... from the console
        return getattr(self.stream, name)


sys.stdout = Tee(sys.stdout, HERE / "server.log")


# ==========================================
# CONFIGURATION (.env file or environment)
# ==========================================
def load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def ask_for_key():
    """First run: ask for the Groq key and keep it in server/.env."""
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print("\nNo Groq API key found.")
    print("Get a free key at https://console.groq.com/keys")
    key = input("Paste your Groq API key (starts with gsk_): ").strip()
    if not key.startswith("gsk_"):
        return None
    with ENV_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\nGROQ_API_KEY={key}\n")
    print(f"Saved to {ENV_FILE}")
    return key


load_env_file()

KEY = os.environ.get("GROQ_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "5000"))
DISCOVERY_PORT = int(os.environ.get("DISCOVERY_PORT", "5001"))
LANGUAGE = os.environ.get("LANGUAGE", "en")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "openai/gpt-oss-20b")
TTS_MODEL = os.environ.get("TTS_MODEL", "canopylabs/orpheus-v1-english")
TTS_VOICE = os.environ.get("TTS_VOICE", "austin")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a friendly voice assistant speaking through a small speaker. "
    "Answer in one or two short sentences, under 40 words. "
    "Use plain spoken English only: no lists, markdown, emojis, URLs or code. "
    "If you need live data such as weather or news, say you can't access it.",
)

# Optional wake word, e.g. WAKE_WORD=jarvis. When set, only sentences that
# contain it get an answer (stops replies to people talking nearby).
# Extra spellings Whisper might produce can follow, comma-separated:
# WAKE_WORD=jarvis,jervis,travis. Follow-ups within FOLLOWUP_SECONDS of an
# answer don't need it.
WAKE_WORDS = [w.strip().lower() for w in os.environ.get("WAKE_WORD", "").split(",")
              if w.strip()]
WAKE_WORD = WAKE_WORDS[0] if WAKE_WORDS else ""
FOLLOWUP_SECONDS = int(os.environ.get("FOLLOWUP_SECONDS", "30"))

BASE = "https://api.groq.com/openai/v1"
TTS_CHUNK_CHARS = 195       # Orpheus accepts at most 200 characters
MAX_TTS_CHUNKS = 4          # caps reply length at roughly 15 seconds
HISTORY_TURNS = 4           # remembered question/answer pairs per device
HISTORY_IDLE_SECONDS = 300  # forget the conversation after 5 quiet minutes

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
session = requests.Session()


def auth():
    return {"Authorization": f"Bearer {KEY}"}


# ==========================================
# WAV HELPERS
# ==========================================
def parse_wav(data):
    """Return (channels, rate, bits, pcm). Tolerates streaming headers
    whose data size is 0xFFFFFFFF, as Groq TTS sends."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = pos + 8
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack(
                "<HHIIHH", data[body:body + 16])
            # 0xFFFE = WAVE_FORMAT_EXTENSIBLE (macOS `say` may write it); its
            # sub-format GUID starts with the real format code.
            if tag == 0xFFFE and size >= 40:
                tag = struct.unpack("<H", data[body + 24:body + 26])[0]
            if tag != 1:
                raise ValueError(f"WAV format {tag} is not PCM")
            fmt = (channels, rate, bits)
        elif cid == b"data":
            if fmt is None:
                raise ValueError("WAV data before fmt chunk")
            end = len(data) if size == 0xFFFFFFFF else min(len(data), body + size)
            pcm = data[body:end]
            align = fmt[0] * fmt[2] // 8
            return (*fmt, pcm[:len(pcm) - len(pcm) % align])
        pos = body + size + (size & 1)
    raise ValueError("WAV has no data chunk")


def make_wav(channels, rate, bits, pcm):
    align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                rate * align, align, bits)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def decode_ima_adpcm(body):
    """Glasses upload format (4x smaller than WAV): b"IMA1", uint32 sample count,
    uint32 sample rate, then IMA/DVI ADPCM nibbles (high nibble first, start state
    0/0, as Python's audioop expects). Returns a 16-bit mono WAV."""
    if len(body) < 12:
        raise ValueError("too short")
    samples, rate = struct.unpack("<II", body[4:12])
    packed = body[12:]
    if not 8000 <= rate <= 48000 or len(packed) * 2 < samples:
        raise ValueError("inconsistent header")
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # C decoder (Python <= 3.12)
        pcm, _ = audioop.adpcm2lin(packed, 2, None)
    except ImportError:
        pcm = _adpcm_decode_py(packed)
    return make_wav(1, rate, 16, pcm[:samples * 2])


_IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
_IMA_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55,
             60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
             337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411,
             1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
             5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500,
             20350, 22385, 24623, 27086, 29794, 32767]


def _adpcm_decode_py(packed):
    """Pure-Python decoder for when audioop is unavailable (Python 3.13+)."""
    pred, index, out = 0, 0, bytearray()
    for byte in packed:
        for code in (byte >> 4, byte & 15):
            step = _IMA_STEP[index]
            delta = step >> 3
            if code & 4:
                delta += step
            if code & 2:
                delta += step >> 1
            if code & 1:
                delta += step >> 2
            pred = max(-32768, min(32767, pred - delta if code & 8 else pred + delta))
            index = max(0, min(88, index + _IMA_INDEX[code]))
            out += struct.pack("<h", pred)
    return bytes(out)


def wav_seconds(data):
    try:
        ch, rate, bits, pcm = parse_wav(data)
        return len(pcm) / (rate * ch * bits // 8)
    except ValueError:
        return 0.0


# ==========================================
# TEXT CLEANUP FOR SPEECH
# ==========================================
PUNCT_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": ", ", "…": "...", " ": " ", " ": " ",
    "°": " degrees",
})

# Whisper "hears" these in silence or noise.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thank you very much",
    "thanks for watching", "thank you for watching", "bye", "okay",
    "subtitles by the amaraorg community", "music",
}


# Background noise often comes back as just these.
FILLER_WORDS = {"hmm", "hm", "mm", "mhm", "um", "uh", "ah", "oh", "so", "okay",
                "ok", "the", "a", "and", "huh"}


def is_real_speech(text):
    words = re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()
    if not words or words in HALLUCINATIONS:
        return False
    return not all(w in FILLER_WORDS for w in words.split())


# "Sounds like Danny": Whisper writes the name as Danny, Denny, Renny, Rani, Ronny,
# Dani, Hirani ("hey Rani" run together) ... Accept either of the first two words
# shaped like consonant + vowel + n(n) + y/ie/i/ey, optionally glued to hey/hi.
# Set WAKE_FUZZY=0 to use only the exact WAKE_WORD spellings.
WAKE_FUZZY = os.environ.get("WAKE_FUZZY", "1") != "0"
FUZZY_NAME = re.compile(r"^(?:hey|hay|hi|he)?[dtrn][aeiou]n{1,2}(?:y|ie|i|ey|e)?$")
FUZZY_AFTER_HEY = re.compile(r"^[aeiou]n{1,2}(?:y|ie|i|ey)$")   # "hey any", "hey anny"
GREETINGS = {"hey", "hi", "hay", "he", "a", "ok", "okay", "yo"}


def fuzzy_wake_index(words):
    """Index of a Danny-sounding word among the first 2 words, else None."""
    for i, word in enumerate(words[:2]):
        if FUZZY_NAME.match(word):
            return i
        if i and words[i - 1] in GREETINGS and FUZZY_AFTER_HEY.match(word):
            return i
    return None


def strip_wake_word(text):
    """Return the question without the wake word, or None if it's absent."""
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()
    if WAKE_FUZZY:
        i = fuzzy_wake_index(words)
        if i is not None:
            # Drop everything up to and including the name ("Hey, Renny. What ..." -> "What ...").
            tokens = re.findall(r"\S+", text)
            kept, seen = [], 0
            for tok in tokens:
                if re.sub(r"[^a-z0-9]+", "", tok.lower()):
                    seen += 1
                if seen > i + 1:
                    kept.append(tok)
            return " ".join(kept).lstrip(" ,.!?").strip()
    for spelling in WAKE_WORDS:
        wake = spelling.split()
        for i in range(len(words) - len(wake) + 1):
            if words[i:i + len(wake)] == wake:
                # Drop everything up to and including the wake word
                # ("Hey Jarvis, what's ..." -> "what's ...").
                pattern = (r"^.*?\b" + r"\W+".join(map(re.escape, wake))
                           + r"\b[\s,.!?]*")
                return re.sub(pattern, "", text, count=1, flags=re.I | re.S).strip()
    return None


def clean_for_speech(text):
    text = unicodedata.normalize("NFKC", text).translate(PUNCT_MAP)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # markdown links
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*(?:[-*+#>]+|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"[*_`#|~]", "", text)
    text = text.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip()


def split_for_tts(text):
    """Split into <=195-char pieces at sentence, then comma, then word
    boundaries."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces = []
    for sentence in sentences:
        while len(sentence) > TTS_CHUNK_CHARS:
            cut = max(sentence.rfind(", ", 0, TTS_CHUNK_CHARS),
                      sentence.rfind("; ", 0, TTS_CHUNK_CHARS))
            if cut < 60:
                cut = sentence.rfind(" ", 0, TTS_CHUNK_CHARS)
            if cut <= 0:
                cut = TTS_CHUNK_CHARS
            pieces.append(sentence[:cut + 1].strip())
            sentence = sentence[cut + 1:].strip()
        if sentence:
            pieces.append(sentence)

    chunks = []
    for piece in pieces:
        if chunks and len(chunks[-1]) + 1 + len(piece) <= TTS_CHUNK_CHARS:
            chunks[-1] += " " + piece
        else:
            chunks.append(piece)
    return chunks[:MAX_TTS_CHUNKS]


# ==========================================
# GROQ CALLS
# ==========================================
class StageError(Exception):
    def __init__(self, stage, status, detail):
        super().__init__(f"{stage} failed ({status}): {detail}")
        self.stage = stage


def groq_error(r):
    try:
        return r.json()["error"]["message"]
    except Exception:
        return r.text[:300]


def transcribe(wav_bytes):
    data = {"model": STT_MODEL, "response_format": "json", "temperature": "0"}
    if LANGUAGE:
        data["language"] = LANGUAGE
    r = session.post(
        f"{BASE}/audio/transcriptions", headers=auth(), data=data,
        files={"file": ("input.wav", wav_bytes, "audio/wav")}, timeout=60)
    if not r.ok:
        raise StageError("Speech recognition", r.status_code, groq_error(r))
    return r.json().get("text", "").strip()


def chat(history, text):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages += list(history)
    messages.append({"role": "user", "content": text})
    body = {"model": CHAT_MODEL, "messages": messages,
            "max_completion_tokens": 1024}
    if CHAT_MODEL.startswith("openai/gpt-oss"):
        # Keeps the reasoning short so it doesn't use up the token budget
        # and leave an empty answer.
        body["reasoning_effort"] = "low"
    elif CHAT_MODEL.startswith("qwen/"):
        # Qwen thinks before answering by default; off is ~3x faster for short replies.
        body["reasoning_effort"] = "none"
    r = session.post(f"{BASE}/chat/completions", headers=auth(), json=body,
                     timeout=60)
    if not r.ok:
        raise StageError("Chat", r.status_code, groq_error(r))
    message = (r.json().get("choices") or [{}])[0].get("message") or {}
    reply = message.get("content") or ""
    if isinstance(reply, list):
        reply = " ".join(p.get("text", "") for p in reply if isinstance(p, dict))
    return reply.strip()


def speak_chunk(text):
    r = session.post(
        f"{BASE}/audio/speech", headers=auth(), timeout=60,
        json={"model": TTS_MODEL, "voice": TTS_VOICE, "input": text,
              "response_format": "wav"})
    if not r.ok:
        raise StageError("Voice generation", r.status_code, groq_error(r))
    return parse_wav(r.content)


import base64
import subprocess
import tempfile

# One long-lived PowerShell keeps the Windows synthesizer loaded, so each reply
# skips PowerShell start-up (~0.25 s). Lines in: base64(text) TAB wav-path.
TTS_WORKER_SCRIPT = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
  [System.Speech.AudioFormat.AudioChannel]::Mono)
[Console]::Out.WriteLine('ready'); [Console]::Out.Flush()
while ($true) {
  $line = [Console]::In.ReadLine()
  if ($line -eq $null) { break }
  $parts = $line.Split([char]9)
  $text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($parts[0]))
  try { $s.SetOutputToWaveFile($parts[1], $f); $s.Speak($text); $s.SetOutputToNull()
        [Console]::Out.WriteLine('done') }
  catch { [Console]::Out.WriteLine('error') }
  [Console]::Out.Flush()
}
"""
_tts_proc = None
_tts_lock = threading.Lock()


def _tts_worker():
    global _tts_proc
    if _tts_proc is None or _tts_proc.poll() is not None:
        encoded = base64.b64encode(TTS_WORKER_SCRIPT.encode("utf-16-le")).decode()
        _tts_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        if _tts_proc.stdout.readline().strip() != "ready":
            _tts_proc.kill()
            _tts_proc = None
            raise RuntimeError("Windows voice worker did not start")
    return _tts_proc


def windows_tts(text):
    """Fallback voice: Windows' built-in speech synthesizer, 16 kHz mono WAV.
    Used when Groq refuses Orpheus (e.g. terms not accepted for this key's org)."""
    global _tts_proc
    with _tts_lock, tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "reply.wav")
        try:
            worker = _tts_worker()
            worker.stdin.write(base64.b64encode(text.encode("utf-8")).decode() + "\t" + path + "\n")
            worker.stdin.flush()
            if worker.stdout.readline().strip() == "done":
                with open(path, "rb") as f:
                    return f.read()
        except (OSError, RuntimeError, ValueError):
            pass
        if _tts_proc is not None:  # worker broke: restart it next time, use one-shot now
            _tts_proc.kill()
            _tts_proc = None
        return windows_tts_oneshot(text)


def windows_tts_oneshot(text):
    """Slower fallback: a fresh PowerShell per reply."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "reply.wav")
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$f = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,"
            "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
            "[System.Speech.AudioFormat.AudioChannel]::Mono);"
            "$s.SetOutputToWaveFile($env:TTS_OUT, $f); $s.Speak($env:TTS_TEXT); $s.Dispose()"
        )
        # Text goes through an environment variable, never into the script itself.
        env = dict(os.environ, TTS_TEXT=text, TTS_OUT=path)
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       check=True, env=env, timeout=60, capture_output=True)
        with open(path, "rb") as f:
            return f.read()


def mac_tts(text):
    """Fallback voice on macOS: the built-in `say` command, 16 kHz mono 16-bit WAV.
    MAC_VOICE=Samantha (any name from `say -v ?`) picks the voice."""
    with tempfile.TemporaryDirectory() as tmp:
        text_file = os.path.join(tmp, "reply.txt")
        path = os.path.join(tmp, "reply.wav")
        with open(text_file, "w", encoding="utf-8") as f:  # -f: text never parsed as options
            f.write(text)
        cmd = ["say", "-f", text_file, "-o", path,
               "--file-format=WAVE", "--data-format=LEI16@16000"]
        voice = os.environ.get("MAC_VOICE", "").strip()
        if voice:
            cmd[1:1] = ["-v", voice]
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        with open(path, "rb") as f:
            raw = f.read()
    # Rewrite as a plain 44-byte PCM WAV: Apple files can carry extra chunks
    # (e.g. FLLR padding) or the "extensible" format tag.
    return make_wav(*parse_wav(raw))


def system_voice_name():
    """Which built-in voice can stand in for Orpheus on this computer, or None."""
    choice = os.environ.get("TTS_FALLBACK", "system").strip().lower()
    if choice in ("off", "none", "0"):
        return None
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "macOS"
    return None  # Linux etc.: no built-in fallback; Orpheus errors are reported


def system_tts(text):
    return windows_tts(text) if os.name == "nt" else mac_tts(text)


fallback_noted = False
orpheus_retry_at = 0.0       # while time < this, skip Orpheus and use the system voice
ORPHEUS_RETRY_SECONDS = 600  # re-check Orpheus every 10 minutes (e.g. after accepting terms)


def synthesize(text):
    global fallback_noted, orpheus_retry_at
    chunks = split_for_tts(text)
    if not chunks:
        raise StageError("Voice generation", 0, "nothing to say")
    fallback = system_voice_name()
    if fallback and time.time() < orpheus_retry_at:
        return system_tts(" ".join(chunks))  # Orpheus refused recently: don't waste a round trip
    try:
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            parts = list(pool.map(speak_chunk, chunks))
    except StageError as e:
        if not fallback:
            raise
        orpheus_retry_at = time.time() + ORPHEUS_RETRY_SECONDS
        if not fallback_noted:
            print(f"  NOTE: {e}\n  -> using the {fallback} voice (Orpheus re-checked every "
                  f"{ORPHEUS_RETRY_SECONDS // 60} min)")
            fallback_noted = True
        return system_tts(" ".join(chunks))
    fallback_noted = False
    orpheus_retry_at = 0.0
    channels, rate, bits, _ = parts[0]
    gap = b"\x00" * (int(rate * 0.12) * channels * bits // 8)
    pcm = bytearray()
    for i, (ch, rt, bt, part) in enumerate(parts):
        if (ch, rt, bt) != (channels, rate, bits):
            raise StageError("Voice generation", 0, "mixed audio formats")
        if i:
            pcm += gap
        pcm += part
    return make_wav(channels, rate, bits, bytes(pcm))


# ==========================================
# CONVERSATION MEMORY (per device)
# ==========================================
histories = {}
history_lock = threading.Lock()


def get_history(device):
    with history_lock:
        entry = histories.get(device)
        if not entry or time.time() - entry["t"] > HISTORY_IDLE_SECONDS:
            entry = {"t": time.time(), "msgs": deque(maxlen=HISTORY_TURNS * 2)}
            histories[device] = entry
        return entry


def header_safe(text, limit=300):
    return clean_for_speech(text)[:limit]


def wav_response(wav, heard="", reply=""):
    response = Response(wav, status=200, content_type="audio/wav")
    response.headers["Content-Length"] = str(len(wav))
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Transcript"] = header_safe(heard)
    response.headers["X-Reply"] = header_safe(reply)
    return response


# ==========================================
# ROUTES
# ==========================================
@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok", key=bool(KEY))


@app.route("/sink", methods=["POST"])
def sink():
    """Network test target for the glasses' 'nettest' command: read and discard."""
    request.get_data()
    return Response(status=204)


@app.route("/ask", methods=["POST"])
def ask():
    if not KEY:
        return jsonify(error="Server has no GROQ_API_KEY"), 500

    # The ESP32 sends a raw WAV body; multipart "audio" is accepted too.
    if "audio" in request.files:
        audio = request.files["audio"].read()
    else:
        audio = request.get_data()
    if audio[:4] == b"IMA1":
        try:
            audio = decode_ima_adpcm(audio)
        except ValueError as e:
            return jsonify(error=f"Bad ADPCM body: {e}"), 400
    if len(audio) < 44 or audio[:4] != b"RIFF":
        return jsonify(error="Body must be a WAV file"), 400

    device = request.headers.get("X-Device") or request.remote_addr
    started = time.time()
    # Keep the latest recording so you can listen to what the mic heard.
    try:
        (HERE / "last_question.wav").write_bytes(audio)
        if os.environ.get("SAVE_RECORDINGS") == "1":
            folder = HERE / "recordings"
            folder.mkdir(exist_ok=True)
            (folder / time.strftime("%H%M%S.wav")).write_bytes(audio)
    except OSError:
        pass
    print(f"\n[{time.strftime('%H:%M:%S')}] {device}: "
          f"{len(audio)} bytes, {wav_seconds(audio):.1f}s of audio")

    try:
        t_stt = time.time()
        heard = transcribe(audio)
        print(f"  USER: {heard or '(nothing)'}   [speech-to-text {time.time() - t_stt:.2f}s]")
        if not is_real_speech(heard):
            print("  -> no real speech, ignoring")
            return Response(status=204)

        entry = get_history(device)
        if WAKE_WORD and time.time() - entry.get("answered", 0) > FOLLOWUP_SECONDS:
            question = strip_wake_word(heard)
            if question is None:
                print(f"  -> no wake word \"{WAKE_WORD}\", ignoring")
                return Response(status=204)
            if not is_real_speech(question):
                print("  -> wake word but no question, ignoring")
                return Response(status=204)
            heard = question

        t_chat = time.time()
        reply = clean_for_speech(chat(entry["msgs"], heard))
        if not reply:
            reply = "Sorry, I didn't catch that. Could you say it again?"
        print(f"  ASSISTANT: {reply}   [answer {time.time() - t_chat:.2f}s]")

        t_tts = time.time()
        wav = synthesize(reply)
        print(f"  [voice {time.time() - t_tts:.2f}s]")
        with history_lock:
            entry["msgs"].append({"role": "user", "content": heard})
            entry["msgs"].append({"role": "assistant", "content": reply})
            entry["t"] = entry["answered"] = time.time()

        print(f"  -> {len(wav)} bytes, {wav_seconds(wav):.1f}s of speech, "
              f"total {time.time() - started:.1f}s")
        return wav_response(wav, heard, reply)

    except StageError as e:
        print("  ERROR:", e)
        return jsonify(error=str(e)), 502
    except requests.Timeout:
        print("  ERROR: Groq timed out")
        return jsonify(error="Groq request timed out"), 504
    except requests.RequestException as e:
        print("  NETWORK ERROR:", e)
        return jsonify(error="Cannot reach Groq - check internet"), 502
    except Exception:
        app.logger.exception("Unexpected server error")
        return jsonify(error="Internal server error"), 500


hello_cache = {}


@app.route("/hello", methods=["GET"])
def hello():
    """Short spoken test so the ESP32 can check the download + speaker path."""
    text = "Hi! I'm connected to your laptop and ready. Ask me anything."
    try:
        if "wav" not in hello_cache:
            hello_cache["wav"] = synthesize(text)
        return wav_response(hello_cache["wav"], "", text)
    except StageError as e:
        print("  ERROR:", e)
        return jsonify(error=str(e)), 502
    except requests.RequestException as e:
        print("  NETWORK ERROR:", e)
        return jsonify(error="Cannot reach Groq - check internet"), 502


@app.route("/reset", methods=["POST"])
def reset():
    with history_lock:
        histories.clear()
    return jsonify(status="conversation cleared")


# ==========================================
# AUTO-DISCOVERY (UDP)
# ==========================================
def discovery_responder():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", DISCOVERY_PORT))
    except OSError as e:
        print(f"WARNING: discovery port {DISCOVERY_PORT} unavailable ({e}). "
              "Set the server address on the ESP32 manually.")
        return
    while True:
        try:
            data, addr = sock.recvfrom(64)
        except OSError:
            continue
        if data.strip() == b"VA_DISCOVER":
            sock.sendto(f"VA_SERVER {PORT}".encode(), addr)
            print(f"[discovery] answered ESP32 at {addr[0]}")


# ==========================================
# STARTUP
# ==========================================
def lan_addresses():
    addresses = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except socket.gaierror:
        pass
    try:  # address of the interface that routes to the internet
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addresses.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith(("127.", "169.254.")))


def check_key():
    try:
        r = session.get(f"{BASE}/models", headers=auth(), timeout=10)
    except requests.RequestException as e:
        return f"cannot reach Groq ({e.__class__.__name__}) - check internet"
    if r.status_code == 401:
        return "key rejected (401) - check GROQ_API_KEY"
    if not r.ok:
        return f"Groq error {r.status_code}"
    return "OK"


def main():
    global KEY
    if not KEY:
        KEY = ask_for_key() or ""
    if not KEY:
        sys.exit("Missing GROQ_API_KEY. Put it in server/.env "
                 "(see server/.env.example).")

    print("=" * 52)
    print(" Voice assistant server")
    print("=" * 52)
    print(" Groq key:   ", check_key())
    print(" Chat model: ", CHAT_MODEL)
    print(" Wake word:  ", ", ".join(WAKE_WORDS) if WAKE_WORDS
          else "off (answers everything)")
    print(" Listening on port", PORT, "| discovery on UDP", DISCOVERY_PORT)
    print(" This laptop's addresses (ESP32 finds it automatically):")
    for a in lan_addresses():
        print(f"   http://{a}:{PORT}")
    print("=" * 52)

    print(" Fallback voice:", system_voice_name() or "none (Orpheus only)")
    if system_voice_name() == "Windows":
        # Start the Windows voice now so the first reply doesn't pay for it.
        threading.Thread(target=lambda: windows_tts("ready"), daemon=True).start()
    threading.Thread(target=discovery_responder, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
