"""Call an explicitly configured HTTPS bridge. Never prints transcripts or tokens."""
import argparse
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS bridge origin, without a path")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--ca-file", type=Path, help="Explicit private CA. Omit for system trust.")
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--output", type=Path, help="Explicit opt-in to saving response audio")
    parser.add_argument("--session", default="manual-check")
    args = parser.parse_args()
    url = urlsplit(args.url)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path not in ("", "/") or url.query or url.fragment):
        parser.error("Use an HTTPS origin without user information, path, query or fragment")
    if args.token_file.is_symlink() or (os.name == "posix" and args.token_file.stat().st_mode & 0o077):
        parser.error("Token file must be private, mode 0600")
    if args.token_file.stat().st_size > 512:
        parser.error("Token file is too large")
    headers = {"Authorization": "Bearer " + args.token_file.read_text().strip(), "X-Session": args.session}
    client = requests.Session()
    client.trust_env = False
    try:
        with client.get(args.url.rstrip("/") + "/health", headers=headers, timeout=(5, 10),
                        verify=str(args.ca_file) if args.ca_file else True, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                parser.exit(1, f"Health failed with HTTP {response.status_code}. No response body printed.\n")
        print("Authenticated HTTPS health passed.")
        if args.wav:
            if args.wav.stat().st_size > 330000:
                parser.error("WAV exceeds the bridge upload bound")
            headers["Content-Type"] = "audio/wav"
            started = time.monotonic()
            with client.post(args.url.rstrip("/") + "/ask", headers=headers, data=args.wav.read_bytes(),
                             timeout=(5, 95), verify=str(args.ca_file) if args.ca_file else True,
                             allow_redirects=False, stream=True) as response:
                if response.status_code == 204:
                    print("No speech response, HTTP 204.")
                    return
                if response.status_code != 200 or response.headers.get("Content-Type", "").split(";")[0] != "audio/wav":
                    parser.exit(1, f"Voice request failed with HTTP {response.status_code}.\n")
                output = bytearray()
                for chunk in response.iter_content(16384):
                    output.extend(chunk)
                    if len(output) > 1440044 or time.monotonic() - started > 100:
                        parser.exit(1, "Response exceeded a size or time bound.\n")
                if args.output:
                    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(output)
                print(f"Voice response received, {len(output)} bytes, {time.monotonic()-started:.2f} seconds.")
    except (requests.RequestException, OSError):
        parser.exit(1, "HTTPS operation failed. No insecure retry or response details printed.\n")
    finally:
        client.close()


if __name__ == "__main__":
    main()
