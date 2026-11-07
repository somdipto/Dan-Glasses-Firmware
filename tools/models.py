"""Download only the reviewed public models. No speech APIs, accounts or paid credits."""
import argparse
import hashlib
import json
import os
import ssl
import tempfile
import urllib.request
from pathlib import Path
import certifi

VOICE_REV = "e7229f7d33a10e145e109b886d661984201ac3c4"
VOICE_BASE = f"https://huggingface.co/rhasspy/piper-voices/resolve/{VOICE_REV}/en/en_US/ljspeech/medium/"
FILES = (
    ("ggml-tiny.en-q5_1.bin", "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en-q5_1.bin",
     "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b", 32166155),
    ("en_US-ljspeech-medium.onnx", VOICE_BASE + "en_US-ljspeech-medium.onnx",
     "6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22", 63531379),
    ("en_US-ljspeech-medium.onnx.json", VOICE_BASE + "en_US-ljspeech-medium.onnx.json",
     "141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860", 4972),
)


class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise ValueError("Rejected non-HTTPS model redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPSOnly(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
    for name, url, expected, maximum in FILES:
        target = directory / name
        if target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if expected and digest != expected:
                raise ValueError("Existing model checksum mismatch; not overwriting")
            print(f"Present: {name}; sha256={digest}")
            continue
        fd, temporary = tempfile.mkstemp(prefix=".download-", dir=directory)
        try:
            digest = hashlib.sha256()
            count = 0
            with os.fdopen(fd, "wb") as output, opener.open(url, timeout=60) as response:
                while chunk := response.read(65536):
                    count += len(chunk)
                    if count > maximum:
                        raise ValueError("Model exceeds expected size")
                    digest.update(chunk)
                    output.write(chunk)
            actual = digest.hexdigest()
            if expected and (actual != expected or count != maximum):
                raise ValueError("Model checksum mismatch")
            if name.endswith(".json"):
                config = json.loads(Path(temporary).read_text())
                if config["audio"]["sample_rate"] != 22050:
                    raise ValueError("Unexpected voice sample rate")
            os.replace(temporary, target)
            print(f"Prepared: {name}; bytes={count}; sha256={actual}")
        finally:
            Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("models"))
    args = parser.parse_args()
    try:
        fetch(args.directory)
    except Exception:
        parser.exit(1, "Model preparation failed. No fallback or unverified model was accepted.\n")
