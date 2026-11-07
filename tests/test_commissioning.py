"""Offline approval/privacy/diagnostic boundaries; no serial device is opened.

Records are fixed examples of the current firmware's actual diagnostic grammar,
not an echo server and not physical proof. Run via unittest after integration.
"""
import argparse
import io
import json
import sys
import types
import unittest
from unittest.mock import Mock, patch

from tools import commissioning


HEADER = (f"[status] firmware={commissioning.FIRMWARE} wifi=disconnected rssi=0 "
          "bridge_configured=no authenticated=no usb_unlocked=no usb_rx=ready")
SETTINGS = ("[status] mode=auto voice=0 volume=20 sensitivity=5 "
            "gain=16 listening=no mic=none noise=0 threshold=500 psram=8388608 "
            "stream=idle input=0 heap_free=150000 psram_free=7000000")
AUDIO = ("[status] duplex=full aec=off capture=stopped aec_chunk=0 "
         "reference_lead_ms=10 dma_eofs=0 barge_ins=0")
STATUS = (HEADER, SETTINGS, AUDIO)
NONCE = "1" * 32
READY = (f"[ready] firmware={commissioning.FIRMWARE} nonce={NONCE}",)
READY_WRITE = f"ready {NONCE}\n".encode("ascii")
METER = (
    "[mic] speak now - level for 5 s (# = loudness)",
    "[mic]    40 #",
    "[mic]   120 ###",
    "[mic] peak=120 noise=2 start-threshold=500",
)
DIAGNOSTICS = (
    "[micdiag] raw left: -8388608..8388607  right: 0..0",
    "[micdiag] SD line (D0): high 50/50 with pull-up, 0/50 with pull-down",
    "[micdiag] signal detected; this does not prove wiring or electrical safety",
)
PRIVATE = "secret-fixture-SSID-password-token-CA"


def args(**changes):
    values = dict(command="status", port="/dev/offline-fixture", approved_device_access=True,
                  hardware_confirmed=True, approved_audio_test=False)
    values.update(changes)
    return argparse.Namespace(**values)


def encoded(lines):
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.05
        return self.now


class RecordedPort:
    """Finite independent receive records; never synthesizes replies from writes."""
    def __init__(self, records):
        self.records = iter(records)
        self.buffer = b""
        self.writes = []
        self.closed = False
        self.reads = 0
        self.open_state = None

    def open(self):
        self.open_state = (self.port, self.dtr, self.rts)

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        self.buffer = next(self.records)

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, size):
        self.reads += 1
        # Force records across arbitrary read boundaries, including CR/LF.
        count = min(size, 17)
        result, self.buffer = self.buffer[:count], self.buffer[count:]
        return result


class CommissioningTests(unittest.TestCase):
    def serial_fixture(self, records):
        port = RecordedPort(records)
        factory = Mock(return_value=port)
        patcher = patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=factory)})
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = patch.object(commissioning.time, "monotonic", Clock())
        clock.start()
        self.addCleanup(clock.stop)
        nonce = patch.object(commissioning.secrets, "token_hex", return_value=NONCE)
        nonce.start()
        self.addCleanup(nonce.stop)
        port.records = iter([encoded(READY), *records])
        return port, factory

    def test_all_approvals_and_explicit_port_precede_serial_construction(self):
        _, factory = self.serial_fixture([])
        for changes in ({"approved_device_access": False}, {"hardware_confirmed": False},
                        {"port": ""}, {"port": "--other"}, {"port": "/dev/fixture\n"},
                        {"command": "tone", "approved_audio_test": False}):
            with self.subTest(changes=changes), self.assertRaises(commissioning.CommissionError):
                commissioning.commission(args(**changes))
        factory.assert_not_called()

    def test_complete_status_only_exposes_validated_allowlist(self):
        lines = [f"ssid={PRIVATE}", f"password={PRIVATE}", f"token={PRIVATE}",
                 f"ca={PRIVATE}", f"transcript={PRIVATE}", *STATUS]
        port, factory = self.serial_fixture([encoded(lines)])
        result = commissioning.commission(args())
        self.assertEqual(result["status"]["firmware"], commissioning.FIRMWARE)
        self.assertFalse(result["status"]["listening"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(port.writes, [READY_WRITE, b"status\n"])
        self.assertEqual(port.open_state, ("/dev/offline-fixture", False, False))
        self.assertTrue(port.closed)
        factory.assert_called_once_with(port=None, baudrate=115200, timeout=0.1, write_timeout=1)

    def test_exact_version_gate_blocks_any_extra_command(self):
        for version in ("3.4-voice", "4.0-streaming", "4.1-streaming", commissioning.FIRMWARE + "-extra",
                        "old" + commissioning.FIRMWARE, "3.3-greeting"):
            port, _ = self.serial_fixture([encoded((HEADER.replace(commissioning.FIRMWARE, version), SETTINGS, AUDIO))])
            with self.subTest(version=version), self.assertRaisesRegex(
                    commissioning.CommissionError, "firmware_version_mismatch"):
                commissioning.commission(args(command="micdiag"))
            self.assertEqual(port.writes, [READY_WRITE, b"status\n"])
            self.assertTrue(port.closed)

    def test_ambiguous_missing_and_injected_status_fail_closed(self):
        for lines in ((HEADER,), (HEADER, SETTINGS), (*STATUS, HEADER), (*STATUS, AUDIO),
                      (HEADER, SETTINGS + f" ssid={PRIVATE}", AUDIO),
                      (HEADER.replace("wifi=disconnected", f"wifi={PRIVATE}"), SETTINGS, AUDIO),
                      (HEADER, SETTINGS.replace("gain=16", "gain=65"), AUDIO)):
            with self.subTest(lines=lines), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_status(lines)

    def test_streaming_status_exposes_only_bounded_practical_state(self):
        status = commissioning.parse_status((HEADER, SETTINGS.replace(
            "stream=idle input=0", "stream=draining input=320000"), AUDIO))
        self.assertEqual(status["stream"], "draining")
        self.assertEqual(status["input_bytes"], 320000)
        self.assertEqual(status["heap_free_bytes"], 150000)
        for settings in (SETTINGS.replace("input=0", "input=320001"),
                         SETTINGS.replace("stream=idle", "stream=unknown"),
                         SETTINGS.replace("heap_free=150000", "heap_free=4294967296")):
            with self.subTest(settings=settings), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_status((HEADER, settings, AUDIO))

    def test_duplex_status_rejects_inconsistent_health_and_counter_overflow(self):
        for audio in (AUDIO.replace("capture=stopped", "capture=live"),
                      AUDIO.replace("aec=off", "aec=fd"),
                      AUDIO.replace("aec_chunk=0", "aec_chunk=1025"),
                      AUDIO.replace("dma_eofs=0", "dma_eofs=4294967296"),
                      AUDIO.replace("barge_ins=0", "barge_ins=4294967296")):
            with self.subTest(audio=audio), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_status((HEADER, SETTINGS, audio))

    def test_extra_checks_refuse_active_listening_without_state_commands(self):
        for command in ("mic", "micdiag", "tone"):
            port, _ = self.serial_fixture([encoded((HEADER, SETTINGS.replace("listening=no", "listening=yes"), AUDIO))])
            with self.subTest(command=command), self.assertRaisesRegex(
                    commissioning.CommissionError, "paused_listening_required"):
                commissioning.commission(args(command=command, approved_audio_test=True))
            self.assertEqual(port.writes, [READY_WRITE, b"status\n"])

    def test_tone_never_changes_muted_or_high_stored_volume(self):
        for volume in (0, 21, 100):
            port, _ = self.serial_fixture([encoded((HEADER, SETTINGS.replace("volume=20", f"volume={volume}"), AUDIO))])
            with self.subTest(volume=volume), self.assertRaisesRegex(
                    commissioning.CommissionError, "quiet_nonzero_volume_required"):
                commissioning.commission(args(command="tone", approved_audio_test=True))
            self.assertEqual(port.writes, [READY_WRITE, b"status\n"])

    def test_tone_completion_remains_audibly_and_physically_unverified(self):
        port, _ = self.serial_fixture([encoded(STATUS), encoded((f"[other] {PRIVATE}", "[tone] done")), encoded(STATUS)])
        result = commissioning.commission(args(command="tone", approved_audio_test=True))
        self.assertEqual(result["diagnostic"]["duration_seconds"], 0.5)
        self.assertEqual(result["diagnostic"]["audibility"], "unverified")
        self.assertEqual(result["physical_result"], "unverified")
        self.assertFalse(result["status_after"]["listening"])
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(port.writes, [READY_WRITE, b"status\n", b"tone\n", b"status\n"])

    def test_completed_checks_require_final_paused_status(self):
        port, _ = self.serial_fixture([encoded(STATUS), encoded(METER),
                                      encoded((HEADER, SETTINGS.replace("listening=no", "listening=yes"), AUDIO))])
        with self.assertRaisesRegex(commissioning.CommissionError, "listening_state_changed"):
            commissioning.commission(args(command="mic"))
        self.assertTrue(port.closed)
        self.assertNotIn(b"resume\n", port.writes)

    def test_meter_requires_real_summary_samples_and_consistent_peak(self):
        result = commissioning.parse_meter((*METER, f"[mic] password={PRIVATE}"))
        self.assertEqual(result, {"duration_seconds": 5, "sample_count": 2, "peak": 120,
                                  "noise": 2, "start_threshold": 500})
        for lines in (METER[:-1], METER[1:], (METER[0], METER[-1]),
                      (*METER[:-1], METER[-1].replace("peak=120", "peak=119")),
                      (*METER[:-1], METER[-1] + f" token={PRIVATE}")):
            with self.subTest(lines=lines), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_meter(lines)

    def test_capture_fault_cannot_be_reported_as_a_successful_measurement(self):
        failure = "[mic] incomplete duplex frame; capture discarded cause=rx_overflow"
        for parser, records in ((commissioning.parse_meter, METER),
                                (commissioning.parse_diagnostics, DIAGNOSTICS)):
            with self.subTest(parser=parser.__name__), self.assertRaisesRegex(
                    commissioning.CommissionError, "microphone_capture_incomplete"):
                parser((*records, failure))

    def test_diagnostics_parse_signed_raw_24bit_ranges_and_pull_counts(self):
        result = commissioning.parse_diagnostics((*DIAGNOSTICS, f"[other] {PRIVATE}"))
        self.assertEqual(result["left_min"], -8388608)
        self.assertEqual(result["left_max"], 8388607)
        self.assertEqual(result["sd_pull_up_high"], 50)
        self.assertEqual(result["result"], "signal_detected")
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_diagnostics_cannot_invent_signal_from_partial_or_inconsistent_records(self):
        for lines in (DIAGNOSTICS[:-1], DIAGNOSTICS[1:], (*DIAGNOSTICS, DIAGNOSTICS[0]),
                      (DIAGNOSTICS[0].replace("-8388608", "-8388609"), *DIAGNOSTICS[1:]),
                      (DIAGNOSTICS[0], DIAGNOSTICS[1].replace("50/50", "51/50"), DIAGNOSTICS[2]),
                      (DIAGNOSTICS[0].replace("-8388608..8388607", "0..0"), *DIAGNOSTICS[1:]),
                      (*DIAGNOSTICS, "[micdiag] I2S failed"),
                      (*DIAGNOSTICS[:-1], DIAGNOSTICS[-1] + f" token={PRIVATE}")):
            with self.subTest(lines=lines), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_diagnostics(lines)

    def test_tone_does_not_treat_silence_substrings_or_errors_as_completion(self):
        for lines in ([], ["prefix [tone] done"], [f"[tone] done token={PRIVATE}"],
                      ["[tone] speaker I2S failed", "[tone] done"], ["[tone] done", "[tone] done"]):
            with self.subTest(lines=lines), self.assertRaises(commissioning.CommissionError):
                commissioning.parse_tone(lines)

    def test_floating_diagnostic_reports_observed_fault_not_hardware_pass(self):
        lines = (
            "[micdiag] raw left: 0..0  right: 0..0",
            "[micdiag] SD line (D0): high 50/50 with pull-up, 0/50 with pull-down",
            "[micdiag] RESULT: the mic is not driving SD (line is floating). Check: mic VDD->3V3 and GND->GND (power), SCK->D1 and WS->D2 (clock), and the SD->D0 wire itself.",
        )
        self.assertEqual(commissioning.parse_diagnostics(lines)["result"], "floating")

    def test_receive_bounds_noise_and_does_not_accept_unterminated_status(self):
        port, _ = self.serial_fixture([encoded(STATUS).rstrip(b"\r\n")])
        with self.assertRaises(commissioning.CommissionError):
            commissioning.commission(args())
        self.assertLess(port.reads, 200)
        port, _ = self.serial_fixture([b"x" * 600 + b"\n"])
        with self.assertRaisesRegex(commissioning.CommissionError, "receive_limit_exceeded"):
            commissioning.commission(args())

    def test_byte_flood_and_silent_port_have_finite_receives(self):
        port, _ = self.serial_fixture([b"x" * (commissioning.MAX_BYTES + 1)])
        port.reset_input_buffer()
        # Fast flood crosses the byte cap before the clock deadline.
        port.read = lambda size: b"x" * size
        with self.assertRaisesRegex(commissioning.CommissionError, "receive_limit_exceeded"):
            commissioning.receive(port, 100)
        silent, _ = self.serial_fixture([b""])
        with self.assertRaises(commissioning.CommissionError):
            commissioning.commission(args())
        self.assertLess(silent.reads, 200)

    def test_serial_exception_text_is_never_emitted(self):
        port, _ = self.serial_fixture([b""])
        port.read = Mock(side_effect=OSError(PRIVATE))
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = commissioning.main(["status", "--port", "/dev/offline-fixture",
                                       "--approved-device-access", "--hardware-confirmed"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue()),
                         {"error": "serial_operation_failed", "physical_result": "unverified"})
        self.assertNotIn(PRIVATE, output.getvalue())
        self.assertTrue(port.closed)

    def test_readiness_discards_stale_status_and_waits_through_configured_boot(self):
        port, _ = self.serial_fixture([encoded(STATUS)])
        port.buffer = encoded((*STATUS, f"[ready] firmware={commissioning.FIRMWARE} nonce={NONCE}"))
        original_read = port.read
        clock = Clock()
        writes_at = []
        original_write = port.write

        def write(data):
            writes_at.append(clock.now)
            return original_write(data)

        def delayed(size):
            # Model Wi-Fi/NTP/health occupying setup long after the old 2 s
            # timeout. No readiness or fresh status is available before 50 s.
            if port.writes == [READY_WRITE] and clock.now < 50:
                return b""
            return original_read(size)

        port.write, port.read = write, delayed
        with patch.object(commissioning.time, "monotonic", clock):
            result = commissioning.commission(args())
        self.assertEqual(result["status"]["firmware"], commissioning.FIRMWARE)
        self.assertGreaterEqual(writes_at[0], commissioning.BOOT_SETTLE_SECONDS)
        self.assertGreaterEqual(writes_at[1], 50)
        self.assertEqual(port.writes, [READY_WRITE, b"status\n"])

    def test_initial_readiness_never_uses_stale_nonce_or_legacy_version(self):
        for response, code in (
                (encoded(STATUS), "initial_readiness_timeout"),
                (encoded((READY[0].replace(NONCE, "2" * 32),)), "initial_readiness_timeout"),
                (encoded((READY[0].replace(commissioning.FIRMWARE, "4.1-streaming"),)), "firmware_version_mismatch"),
                (encoded((*READY, *READY)), "incomplete_or_ambiguous_readiness"),
                (encoded(READY) + f"[ready] firmware={commissioning.FIRMWARE}".encode("ascii"), "incomplete_or_ambiguous_readiness")):
            port, _ = self.serial_fixture([])
            port.records = iter([response])
            with self.subTest(response=response), self.assertRaisesRegex(commissioning.CommissionError, code):
                commissioning.commission(args())
            self.assertEqual(port.writes, [READY_WRITE])
            self.assertTrue(port.closed)

    def test_usb_rx_allocation_failure_is_reported_without_claiming_unlock(self):
        result = commissioning.parse_status((HEADER.replace("usb_rx=ready", "usb_rx=failed"), SETTINGS, AUDIO))
        self.assertFalse(result["usb_receive_ready"])
        self.assertFalse(result["usb_unlocked"])


if __name__ == "__main__":
    unittest.main()
