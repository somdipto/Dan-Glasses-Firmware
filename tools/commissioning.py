"""Explicit, bounded physical checks for firmware 5.2-duplex.

Examples (only after electrical review and separate access approval)::

    python3 tools/commissioning.py status --port PORT --approved-device-access --hardware-confirmed
    python3 tools/commissioning.py mic --port PORT --approved-device-access --hardware-confirmed
    python3 tools/commissioning.py micdiag --port PORT --approved-device-access --hardware-confirmed
    python3 tools/commissioning.py tone --port PORT --approved-device-access --hardware-confirmed --approved-audio-test

Mic checks require an already-paused device; tone also requires stored volume
1..20. This tool never changes settings or resumes listening. The firmware's
meter captures aggregate levels for 5 seconds; its tone is 1 kHz for 0.5 seconds
plus a silence tail. A quiet digital setting is NOT proof of safe ear-level SPL:
keep the speaker away from ears. Completion is not physical/audible PASS.
Serial input is transient: only exact allowlisted fields are emitted, never logs.
No reset, BOOT, flash, NVS writes, connection commands, providers or audio files.
Firmware may independently run background network work; this is not a network
isolation tool. Status does not pause an already-listening device.
The initial USB phase discards boot output for 3 seconds, then waits at most
120 seconds for a fresh nonce-bound setup-complete reply before requesting
status. This includes configured Wi-Fi/NTP/health startup, not bridge approval.
"""
import argparse
import json
import re
import secrets
import sys
import time

FIRMWARE = "5.2-duplex"
COMMAND_SECONDS = {"status": 2.0, "mic": 8.0, "micdiag": 5.0, "tone": 3.0}
MAX_BYTES = 16384
MAX_LINE = 512
MAX_LINES = 128
BOOT_SETTLE_SECONDS = 3.0
READINESS_SECONDS = 120.0


class CommissionError(Exception):
    """A fixed, safe error code; never includes serial or exception text."""


def require(condition, code):
    if not condition:
        raise CommissionError(code)


def validate_access(args):
    require(args.approved_device_access and args.hardware_confirmed, "device_access_not_approved")
    require(isinstance(args.port, str) and 0 < len(args.port) <= 512
            and not args.port.startswith("-")
            and not any(ord(c) <= 32 or ord(c) == 127 for c in args.port), "explicit_port_required")
    require(args.command in COMMAND_SECONDS, "unsupported_command")
    if args.command == "tone":
        require(args.approved_audio_test, "audio_test_not_approved")


def receive(port, seconds):
    """Bound time, bytes, line length and line count, including noisy devices."""
    deadline = time.monotonic() + seconds
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        port.timeout = min(0.1, remaining)
        chunk = port.read(min(256, MAX_BYTES + 1 - len(data)))
        data.extend(chunk)
        require(len(data) <= MAX_BYTES, "receive_limit_exceeded")
    lines = data.split(b"\n")
    require(len(lines) <= MAX_LINES and all(len(line) <= MAX_LINE for line in lines),
            "receive_limit_exceeded")
    # Only complete newline-delimited firmware records count as evidence.
    return [line.removesuffix(b"\r").decode("ascii", "replace") for line in lines[:-1]]


def exchange(port, command):
    port.reset_input_buffer()
    payload = (command + "\n").encode("ascii")
    require(port.write(payload) == len(payload), "serial_write_incomplete")
    # Do not flush: some serial implementations wait indefinitely for drain.
    return receive(port, COMMAND_SECONDS[command])


def initial_readiness(port):
    """One open-session boot phase; never trust boot logs or a cached status."""
    receive(port, BOOT_SETTLE_SECONDS)  # Never send while USB may be initializing.
    port.reset_input_buffer()
    nonce = secrets.token_hex(16)
    payload = ("ready " + nonce + "\n").encode("ascii")
    require(port.write(payload) == len(payload), "serial_write_incomplete")
    deadline = time.monotonic() + READINESS_SECONDS
    data = bytearray()
    records = 0
    total = 0
    ready_at = None
    while True:
        remaining = deadline - time.monotonic()
        require(remaining > 0, "initial_readiness_timeout")
        port.timeout = min(0.1, remaining)
        chunk = port.read(256)
        if ready_at is not None:
            if chunk:
                ready_at = time.monotonic()
            elif time.monotonic() - ready_at >= 0.2:
                require(not data, "incomplete_or_ambiguous_readiness")
                return
        total += len(chunk)
        require(total <= MAX_BYTES, "receive_limit_exceeded")
        data.extend(chunk)
        while b"\n" in data:
            line, _, rest = data.partition(b"\n")
            data = bytearray(rest)
            records += 1
            require(records <= MAX_LINES and len(line) <= MAX_LINE, "receive_limit_exceeded")
            match = re.fullmatch(rb"\[ready\] firmware=([A-Za-z0-9.-]{1,32}) nonce=" +
                                 nonce.encode("ascii"), line.removesuffix(b"\r"))
            if match:
                require(match[1].decode("ascii") == FIRMWARE, "firmware_version_mismatch")
                require(ready_at is None, "incomplete_or_ambiguous_readiness")
                ready_at = time.monotonic()
        require(len(data) <= MAX_LINE, "receive_limit_exceeded")


def bounded(value, low, high):
    number = int(value)
    require(low <= number <= high, "diagnostic_value_out_of_range")
    return number


def parse_status(lines):
    header = re.compile(
        r"\[status\] firmware=([A-Za-z0-9.-]{1,32}) wifi=(connected|disconnected) "
        r"rssi=(-?[0-9]{1,3}) bridge_configured=(yes|no) authenticated=(yes|no) "
        r"usb_unlocked=(yes|no) usb_rx=(ready|failed)")
    settings = re.compile(
        r"\[status\] mode=(auto|button) voice=([0-9]{1,6}) volume=([0-9]{1,3}) "
        r"sensitivity=([0-9]{1,2}) gain=([0-9]{1,2}) "
        r"listening=(yes|no) mic=(none|left|right) noise=([0-9]{1,6}) "
        r"threshold=([0-9]{1,6}) psram=([0-9]{1,10}) "
        r"stream=(idle|connecting|capturing|waiting|speaking|draining|committing|cancelling) "
        r"input=([0-9]{1,6}) heap_free=([0-9]{1,10}) psram_free=([0-9]{1,10})")
    audio = re.compile(
        r"\[status\] duplex=(full) aec=(fd|off) capture=(live|stopped) "
        r"aec_chunk=([0-9]{1,4}) reference_lead_ms=([0-9]{1,3}) "
        r"dma_eofs=([0-9]{1,10}) barge_ins=([0-9]{1,10})")
    first, second, third = [], [], []
    for line in lines:
        if line.startswith("[status]"):
            a, b, c = header.fullmatch(line), settings.fullmatch(line), audio.fullmatch(line)
            require(a is not None or b is not None or c is not None, "invalid_status")
            if a:
                first.append(a.groups())
            if b:
                second.append(b.groups())
            if c:
                third.append(c.groups())
    require(len(first) == len(second) == len(third) == 1, "incomplete_or_ambiguous_status")
    version, wifi, rssi, configured, authenticated, unlocked, usb_rx = first[0]
    require(version == FIRMWARE, "firmware_version_mismatch")
    (mode, voice, volume, sensitivity, gain, listening, mic, noise, threshold,
     psram, stream, input_bytes, heap_free, psram_free) = second[0]
    duplex, aec, capture, chunk, reference_lead, dma_eofs, barge_ins = third[0]
    chunk = bounded(chunk, 0, 1024)
    require((aec == "fd") == (capture == "live"), "invalid_status")
    require(aec != "fd" or chunk > 0, "invalid_status")
    return {
        "firmware": FIRMWARE, "wifi": wifi, "rssi": bounded(rssi, -127, 0),
        "bridge_configured": configured == "yes", "authenticated": authenticated == "yes",
        "usb_unlocked": unlocked == "yes", "mode": mode, "voice": bounded(voice, 0, 32768),
        "usb_receive_ready": usb_rx == "ready",
        "volume": bounded(volume, 0, 100),
        "sensitivity": bounded(sensitivity, 1, 10), "gain": bounded(gain, 1, 64),
        "listening": listening == "yes", "mic": mic, "noise": bounded(noise, 0, 32768),
        "threshold": bounded(threshold, 0, 196608), "psram_bytes": bounded(psram, 0, 2**32 - 1),
        "stream": stream, "input_bytes": bounded(input_bytes, 0, 320000),
        "heap_free_bytes": bounded(heap_free, 0, 2**32 - 1),
        "psram_free_bytes": bounded(psram_free, 0, 2**32 - 1),
        "duplex": duplex, "aec": aec, "capture": capture,
        "aec_chunk_samples": chunk, "reference_lead_ms": bounded(reference_lead, 0, 100),
        "dma_eofs": bounded(dma_eofs, 0, 2**32 - 1),
        "barge_ins": bounded(barge_ins, 0, 2**32 - 1),
    }


def check_capture_errors(lines):
    require("[mic] I2S failed" not in lines and "[micdiag] I2S failed" not in lines,
            "microphone_i2s_failed")
    require(not any(line.startswith("[mic] incomplete duplex frame; capture discarded") for line in lines),
            "microphone_capture_incomplete")


def parse_meter(lines):
    check_capture_errors(lines)
    require(lines.count("[mic] speak now - level for 5 s (# = loudness)") == 1,
            "meter_start_missing")
    sample = re.compile(r"\[mic\] +([0-9]{1,5}) (#{0,50})")
    summary = re.compile(r"\[mic\] peak=([0-9]{1,5}) noise=([0-9]{1,5}) start-threshold=([0-9]{1,6})")
    levels, summaries = [], []
    started = False
    for line in lines:
        if line == "[mic] speak now - level for 5 s (# = loudness)":
            started = True
        match = sample.fullmatch(line)
        if match:
            require(started and not summaries, "invalid_meter_sequence")
            levels.append(bounded(match[1], 0, 32768))
        match = summary.fullmatch(line)
        if match:
            require(started, "invalid_meter_sequence")
            summaries.append(match.groups())
    require(levels and len(summaries) == 1, "meter_summary_missing")
    peak, noise, threshold = summaries[0]
    peak = bounded(peak, 0, 32768)
    require(peak == max(levels), "meter_peak_inconsistent")
    return {"duration_seconds": 5, "sample_count": len(levels), "peak": peak,
            "noise": bounded(noise, 0, 32768), "start_threshold": bounded(threshold, 0, 196608)}


DIAG_RESULTS = {
    "[micdiag] signal detected; this does not prove wiring or electrical safety": "signal_detected",
    "[micdiag] RESULT: the mic is not driving SD (line is floating). Check: mic VDD->3V3 and GND->GND (power), SCK->D1 and WS->D2 (clock), and the SD->D0 wire itself.": "floating",
    "[micdiag] RESULT: SD is stuck LOW even with pull-up - the SD wire is touching GND or sits in a wrong row.": "stuck_low",
    "[micdiag] RESULT: SD is stuck HIGH - the SD wire is touching 3V3/5V or sits in a wrong row.": "stuck_high",
    "[micdiag] RESULT: SD line is unstable - loose contact? Re-seat the SD->D0 wire.": "unstable",
}


def parse_diagnostics(lines):
    check_capture_errors(lines)
    raw = re.compile(r"\[micdiag\] raw left: (-?[0-9]{1,8})\.\.(-?[0-9]{1,8})  right: (-?[0-9]{1,8})\.\.(-?[0-9]{1,8})")
    sd = re.compile(r"\[micdiag\] SD line \(D0\): high ([0-9]{1,2})/50 with pull-up, ([0-9]{1,2})/50 with pull-down")
    ranges = [match.groups() for line in lines if (match := raw.fullmatch(line))]
    pulls = [match.groups() for line in lines if (match := sd.fullmatch(line))]
    results = [DIAG_RESULTS[line] for line in lines if line in DIAG_RESULTS]
    require(len(ranges) == len(pulls) == len(results) == 1, "diagnostics_incomplete_or_ambiguous")
    left_min, left_max, right_min, right_max = [bounded(v, -8388608, 8388607) for v in ranges[0]]
    require(left_min <= left_max and right_min <= right_max, "invalid_diagnostic_range")
    up, down = [bounded(v, 0, 50) for v in pulls[0]]
    detected = left_max - left_min > 100 or right_max - right_min > 100
    expected = "signal_detected" if detected else (
        "floating" if up == 50 and down == 0 else "stuck_low" if up == 0 else
        "stuck_high" if down == 50 else "unstable")
    require(results[0] == expected, "diagnostic_result_inconsistent")
    return {"left_min": left_min, "left_max": left_max, "right_min": right_min,
            "right_max": right_max, "sd_pull_up_high": up, "sd_pull_down_high": down,
            "result": expected}


def parse_tone(lines):
    require("[tone] speaker I2S failed" not in lines, "speaker_i2s_failed")
    require(lines.count("[tone] done") == 1, "tone_completion_missing")
    return {"firmware_reported_done": True, "frequency_hz": 1000, "duration_seconds": 0.5,
            "audibility": "unverified", "ear_level_safety": "unverified"}


def commission(args):
    validate_access(args)  # Must precede even importing/constructing pyserial.
    import serial
    port = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=1)
    try:
        port.port, port.dtr, port.rts = args.port, False, False
        port.open()
        initial_readiness(port)
        status = parse_status(exchange(port, "status"))
        report = {"command": args.command, "status": status, "physical_result": "unverified"}
        if args.command == "status":
            return report
        # Refuse active listening rather than issuing pause/resume or risking an
        # automatic conversation when the firmware's diagnostic returns.
        require(not status["listening"], "paused_listening_required")
        if args.command == "tone":
            require(1 <= status["volume"] <= 20, "quiet_nonzero_volume_required")
        lines = exchange(port, args.command)
        report["diagnostic"] = {"mic": parse_meter, "micdiag": parse_diagnostics,
                                "tone": parse_tone}[args.command](lines)
        final = parse_status(exchange(port, "status"))
        require(not final["listening"], "listening_state_changed")
        if args.command == "tone":
            require(final["volume"] == status["volume"], "volume_state_changed")
        report["status_after"] = final
        return report
    finally:
        port.close()


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CommissionError("invalid_arguments")


def main(argv=None):
    parser = SafeParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=tuple(COMMAND_SECONDS))
    parser.add_argument("--port", required=True)
    parser.add_argument("--approved-device-access", action="store_true")
    parser.add_argument("--hardware-confirmed", action="store_true")
    parser.add_argument("--approved-audio-test", action="store_true")
    try:
        report = commission(parser.parse_args(argv))
    except CommissionError as error:
        print(json.dumps({"error": str(error), "physical_result": "unverified"}))
        return 1
    except Exception:
        # pyserial/OS exception strings may contain sensitive paths or device data.
        print(json.dumps({"error": "serial_operation_failed", "physical_result": "unverified"}))
        return 1
    print(json.dumps(report, sort_keys=True))
    if report.get("diagnostic", {}).get("result") in {"floating", "stuck_low", "stuck_high", "unstable"}:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
