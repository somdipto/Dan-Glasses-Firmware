"""Offline delivery safety boundaries; no real compiler, serial port or upload is used."""
import argparse
import io
import hashlib
import json
import itertools
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from tools import commissioning, device

STATUS_HEADER = (f"[status] firmware={commissioning.FIRMWARE} wifi=disconnected rssi=0 "
                 "bridge_configured=no authenticated=no usb_unlocked=yes usb_rx=ready")
STATUS_SETTINGS = ("[status] mode=auto voice=0 volume=20 sensitivity=5 "
                   "gain=16 listening=no mic=none noise=0 threshold=500 psram=8388608 "
                   "stream=idle input=0 heap_free=150000 psram_free=7000000")
STATUS_AUDIO = ("[status] duplex=full aec=off capture=stopped aec_chunk=0 "
                "reference_lead_ms=10 dma_eofs=0 barge_ins=0")
STATUS = STATUS_HEADER + "\n" + STATUS_SETTINGS + "\n" + STATUS_AUDIO + "\n"
NONCE = "1" * 32
READY_WRITE = f"ready {NONCE}\n".encode("ascii")
READY = f"[ready] firmware={commissioning.FIRMWARE} nonce={NONCE}\n".encode("ascii")


class DeviceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(device, "ROOT", self.root).start()
        self.sketch = self.root / "firmware" / "voice_assistant"
        self.sketch.mkdir(parents=True)
        (self.sketch / "voice_assistant.ino").write_text("void setup() {}\nvoid loop() {}\n")
        (self.sketch / "bridge_protocol.h").write_text("#pragma once\n")
        self.platform = self.root / "packages" / "esp32" / "hardware" / "esp32" / device.CORE_VERSION
        for name in ("platform.txt", "boards.txt", "tools/flasher.py", "tools/partitions/boot_app0.bin"):
            path = self.platform / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture platform input " + name).encode())
        self.library = self.root / "libraries" / "WebSockets"
        (self.library / "src").mkdir(parents=True)
        (self.library / "library.properties").write_text("name=WebSockets\nversion=2.7.1\n")
        (self.library / "src" / "WebSocketsClient.cpp").write_text("// reviewed fixture source\n")
        files = {path.relative_to(self.library).as_posix(): device.file_hash(path)
                 for path in self.library.rglob("*") if path.is_file()}
        digest = hashlib.sha256("".join(name + "\0" + files[name] + "\n"
                                        for name in sorted(files)).encode()).hexdigest()
        patch.object(device, "WEBSOCKETS_SOURCES_SHA256", digest).start()
        patch.object(device, "installed_websocket_library",
                     side_effect=lambda cli: device.websocket_receipt(self.library)).start()
        self.output = device.reviewed_output()
        self.output.mkdir(parents=True)
        self.write_artifacts(self.output)
        self.cli = patch.object(device, "pinned_cli", return_value="/fixture/arduino-cli").start()
        self.active_platform = patch.object(device, "active_platform", return_value=str(self.platform)).start()
        self.run = patch.object(device.subprocess, "run").start()
        patch("sys.stdout", new_callable=io.StringIO).start()

    def write_artifacts(self, output):
        output.mkdir(parents=True, exist_ok=True)
        for name in (*device.UPLOAD_BINARIES, "boot_app0.bin", "voice_assistant.ino.merged.bin"):
            (output / name).write_bytes(("fixture binary " + name).encode())
        (output / "build.options.json").write_text(json.dumps({
            "fqbn": device.FQBN,
            "customBuildProperties": device.BUILD_PROPERTY,
            "hardwareFolders": f"{self.platform},{self.platform}",
            "sketchLocation": str(self.sketch),
        }))
        (output / "compile_commands.json").write_text(json.dumps([{
            "directory": str(self.root), "file": str(self.library / "src" / "WebSocketsClient.cpp"),
            "arguments": ["fixture-g++", "-DNODEBUG_WEBSOCKETS", "-c",
                          str(self.library / "src" / "WebSocketsClient.cpp")],
        }]))

    def record_review(self, output=None):
        output = output or self.output
        (output / device.MANIFEST).write_text(json.dumps(device.build_metadata(output)))

    def flash_args(self, **changes):
        args = dict(port="/dev/fixture-not-a-device", approved_flash=True, hardware_confirmed=True)
        args.update(changes)
        return argparse.Namespace(**args)

    def build_args(self, **changes):
        args = dict(reference_board=False, actual_board=True, hardware_confirmed=True)
        args.update(changes)
        return argparse.Namespace(**args)

    def test_flash_requires_separate_approval_hardware_and_explicit_port(self):
        self.record_review()
        for changes in ({"approved_flash": False}, {"hardware_confirmed": False}, {"port": ""},
                        {"port": "--erase"}, {"port": "/dev/fixture\n"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                device.flash(self.flash_args(**changes))
        self.run.assert_not_called()
        self.cli.assert_not_called()

    def test_missing_and_reference_only_builds_are_not_flashable(self):
        reference = self.root / ".runtime" / "reference-build"
        self.write_artifacts(reference)
        # Even a copied manifest in the reference directory is never selected for upload.
        self.record_review(reference)
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        self.run.assert_not_called()
        self.cli.assert_not_called()

    def test_changed_source_or_upload_inputs_fail_before_upload(self):
        self.record_review()
        paths = [self.sketch / "voice_assistant.ino", self.sketch / "bridge_protocol.h",
                 *(self.output / name for name in device.UPLOAD_BINARIES),
                 self.output / "build.options.json", self.platform / "tools/partitions/boot_app0.bin",
                 self.platform / "platform.txt", self.platform / "boards.txt", self.platform / "tools/flasher.py",
                 self.library / "src" / "WebSocketsClient.cpp", self.library / "library.properties",
                 self.output / "compile_commands.json"]
        for path in paths:
            original = path.read_bytes()
            with self.subTest(path=path.relative_to(self.root)):
                path.write_bytes(original + b"changed")
                try:
                    with self.assertRaises(ValueError):
                        device.flash(self.flash_args())
                finally:
                    path.write_bytes(original)
        self.run.assert_not_called()
        self.cli.assert_not_called()

    def test_added_source_and_unreviewed_uploader_cache_fail_closed(self):
        self.record_review()
        for path in (self.sketch / "new_guard.h", self.output / "voice_assistant.ino_flashed.bin"):
            with self.subTest(name=path.name):
                path.write_bytes(b"unreviewed input")
                try:
                    with self.assertRaises(ValueError):
                        device.flash(self.flash_args())
                finally:
                    path.unlink()
        self.run.assert_not_called()
        self.cli.assert_not_called()

    def test_missing_upload_binary_and_target_mismatch_fail_closed(self):
        self.record_review()
        missing = self.output / device.UPLOAD_BINARIES[0]
        missing.unlink()
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        self.write_artifacts(self.output)
        manifest = self.output / device.MANIFEST
        metadata = json.loads(manifest.read_text())
        for field, value in (("fqbn", "esp32:esp32:other"), ("reviewed_actual_board", False),
                             ("arduino_cli", "unreviewed"), ("esp32_core", "unreviewed")):
            with self.subTest(field=field):
                manifest.write_text(json.dumps(dict(metadata, **{field: value})))
                with self.assertRaises(ValueError):
                    device.flash(self.flash_args())
        self.run.assert_not_called()
        self.cli.assert_not_called()

    def test_custom_build_options_or_local_upload_overrides_are_not_reviewable(self):
        options_path = self.output / "build.options.json"
        options = json.loads(options_path.read_text())
        for field, value in (("fqbn", "esp32:esp32:other"),
                             ("customBuildProperties", "upload.erase_cmd=-e"),
                             ("hardwareFolders", "/unreviewed/platform")):
            with self.subTest(field=field):
                options_path.write_text(json.dumps(dict(options, **{field: value})))
                with self.assertRaises(ValueError):
                    device.build_metadata(self.output)
        options_path.write_text(json.dumps(options))
        for name in ("platform.local.txt", "boards.local.txt"):
            override = self.platform / name
            override.write_text("upload.erase_cmd=-e\n")
            try:
                with self.subTest(override=name), self.assertRaises(ValueError):
                    device.build_metadata(self.output)
            finally:
                override.unlink()
        self.run.assert_not_called()

    def test_library_source_addition_symlink_and_debug_option_fail_closed(self):
        self.record_review()
        path = self.library / "src" / "unreviewed.h"
        path.write_text("// not in audited upstream\n")
        try:
            with self.assertRaises(ValueError):
                device.flash(self.flash_args())
        finally:
            path.unlink()
        source = self.library / "src" / "WebSocketsClient.cpp"
        original = source.read_bytes()
        target = self.root / "copied-source.cpp"
        target.write_bytes(original)
        source.unlink()
        source.symlink_to(target)
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        source.unlink()
        source.write_bytes(original)
        database = self.output / "compile_commands.json"
        entries = json.loads(database.read_text())
        entries[0]["arguments"].remove("-DNODEBUG_WEBSOCKETS")
        database.write_text(json.dumps(entries))
        with self.assertRaises(ValueError):
            device.build_metadata(self.output)
        self.run.assert_not_called()

    def test_review_binds_exact_dependency_and_compile_option(self):
        self.record_review()
        manifest = json.loads((self.output / device.MANIFEST).read_text())
        self.assertEqual(manifest["schema"], 3)
        self.assertEqual(manifest["firmware"], commissioning.FIRMWARE)
        self.assertEqual(manifest["websocket_library"]["version"], "2.7.1")
        self.assertEqual(manifest["websocket_library"]["commit"], device.WEBSOCKETS_COMMIT)
        self.assertIn("compile_commands.json", manifest["artifacts"])
        device.build(self.build_args())
        command = self.run.call_args.args[0]
        self.assertIn(device.BUILD_PROPERTY, command)
        self.assertEqual(command[command.index("--library") + 1], str(self.library))

    def test_symlinked_artifact_is_not_accepted_even_when_bytes_match(self):
        self.record_review()
        artifact = self.output / device.UPLOAD_BINARIES[0]
        target = self.root / "copied-binary.bin"
        target.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(target)
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        self.run.assert_not_called()

    def test_actual_board_build_requires_hardware_attestation(self):
        with self.assertRaises(ValueError):
            device.build(self.build_args(hardware_confirmed=False))
        self.cli.assert_not_called()
        self.run.assert_not_called()

    def test_changed_active_platform_cannot_upload_reviewed_artifacts(self):
        self.record_review()
        self.active_platform.return_value = str(self.root / "different-installed-core")
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        self.run.assert_not_called()

    def test_failed_rebuild_invalidates_old_review(self):
        self.record_review()
        self.run.side_effect = subprocess.CalledProcessError(1, ["fixture compile"])
        with self.assertRaises(subprocess.CalledProcessError):
            device.build(self.build_args())
        self.assertFalse((self.output / device.MANIFEST).exists())

    def test_source_change_during_compile_cannot_be_reviewed(self):
        def change_source(*args, **kwargs):
            (self.sketch / "bridge_protocol.h").write_text("changed during compile\n")
        self.run.side_effect = change_source
        with self.assertRaises(ValueError):
            device.build(self.build_args())
        self.assertFalse((self.output / device.MANIFEST).exists())

    def test_reference_build_cannot_create_flash_review(self):
        device.build(self.build_args(reference_board=True, actual_board=False, hardware_confirmed=False))
        self.assertFalse((self.output / device.MANIFEST).exists())
        self.run.reset_mock()
        with self.assertRaises(ValueError):
            device.flash(self.flash_args())
        self.run.assert_not_called()

    def provision_args(self):
        token = self.root / "fixture.token"
        token.write_text("a" * 43)
        token.chmod(0o600)
        ca = self.root / "fixture.crt"
        ca.write_bytes(b"-----BEGIN CERTIFICATE-----\n" + b"A" * 128 + b"\n-----END CERTIFICATE-----\n")
        return argparse.Namespace(approved_device_write=True, hardware_confirmed=True,
                                  port="/dev/fixture-not-a-device", bridge_url="https://voice.example.test",
                                  token_file=token, ca_file=ca)

    def provision_serial(self, status, followup_status=None):
        port = MagicMock()
        port.__enter__.return_value = port
        port.write.side_effect = len
        pending = bytearray(STATUS.encode("ascii"))  # Never accept buffered unlock.
        records = iter((READY, status.encode("ascii"), (followup_status or status).encode("ascii")))

        def reset():
            pending[:] = next(records)

        def read(size):
            count = min(size, 17)
            data = bytes(pending[:count])
            del pending[:count]
            return data

        port.reset_input_buffer.side_effect = reset
        port.read.side_effect = read
        patch.dict(sys.modules, {"serial": types.SimpleNamespace(Serial=Mock(return_value=port))}).start()
        patch.object(commissioning.time, "monotonic", side_effect=itertools.count(0, 0.05)).start()
        patch.object(commissioning.secrets, "token_hex", return_value=NONCE).start()
        patch.object(device, "hidden_input", side_effect=["fixture-hotspot", "fixture-password"]).start()
        return port

    def test_legacy_locked_spoofed_duplicate_or_truncated_status_never_receives_credentials(self):
        args = self.provision_args()
        for status in (
                "[status] firmware=3.2-handsfree usb_unlocked=yes\n",
                STATUS.replace("usb_unlocked=yes", "usb_unlocked=no"),
                STATUS.replace("usb_rx=ready", "usb_rx=failed"),
                f"ssid=firmware={commissioning.FIRMWARE}\nserver=usb_unlocked=yes\n",
                STATUS.replace(commissioning.FIRMWARE, commissioning.FIRMWARE + "-extra"),
                STATUS.replace(commissioning.FIRMWARE, "4.1-streaming"),
                STATUS.replace(commissioning.FIRMWARE, "3.4-voice"),
                STATUS.replace("listening=no", "listening=yes"),
                STATUS + STATUS_HEADER + "\n", STATUS + STATUS_SETTINGS + "\n",
                STATUS_HEADER + "\n", STATUS.rstrip("\n"),
                f"[status] ssid=firmware={commissioning.FIRMWARE} server=usb_unlocked=yes\n" + STATUS):
            with self.subTest(status=status):
                port = self.provision_serial(status)
                with self.assertRaises(ValueError):
                    device.provision(args)
                self.assertFalse(any(call.args[0].startswith(
                    (b"ssid ", b"pass ", b"bridge ", b"ca ", b"token "))
                    for call in port.write.call_args_list))
                device.hidden_input.assert_not_called()


    def test_persistence_error_or_embedded_ack_stops_before_next_credential(self):
        args = self.provision_args()
        for reply in ("[error] settings write failed\n",
                      "echo [ok] network name saved\n",
                      "[ok] network name saved\n[ok] network name saved\n"):
            with self.subTest(reply=reply):
                port = self.provision_serial(STATUS)
                with patch.object(device, "receive", return_value=reply), self.assertRaises(ValueError):
                    device.provision(args)
                self.assertNotIn(b"pass fixture-password\n",
                                 [call.args[0] for call in port.write.call_args_list])

    def test_first_pairing_requires_fresh_paused_unlock_before_any_secret(self):
        args = self.provision_args()
        locked = STATUS.replace("usb_unlocked=yes", "usb_unlocked=no").replace("listening=no", "listening=yes")
        for after in (locked, STATUS.replace("listening=no", "listening=yes"),
                      STATUS.replace("usb_rx=ready", "usb_rx=failed")):
            with self.subTest(after=after):
                port = self.provision_serial(locked, after)
                with patch.object(device, "receive", return_value="[ok] paired\n"), self.assertRaises(ValueError):
                    device.provision(args)
                self.assertFalse(any(call.args[0].startswith(
                    (b"ssid ", b"pass ", b"bridge ", b"ca ", b"token "))
                    for call in port.write.call_args_list))
                device.hidden_input.assert_not_called()

    def test_configured_bridge_never_uses_software_first_pairing(self):
        args = self.provision_args()
        locked = STATUS.replace("bridge_configured=no", "bridge_configured=yes").replace("usb_unlocked=yes", "usb_unlocked=no")
        port = self.provision_serial(locked)
        with patch.object(device, "hidden_input", return_value=""), self.assertRaises(ValueError):
            device.provision(args)
        self.assertNotIn(b"pair\n", [call.args[0] for call in port.write.call_args_list])
        self.assertFalse(any(call.args[0].startswith(
            (b"ssid ", b"pass ", b"bridge ", b"ca ", b"token "))
            for call in port.write.call_args_list))

    def test_ca_nvs_limit_is_bytes_including_reserved_terminator(self):
        args = self.provision_args()
        marker = b"-----BEGIN CERTIFICATE-----"
        args.ca_file.write_bytes(marker + b"A" * (4000 - len(marker)))
        with patch.object(device, "hidden_input") as prompt, self.assertRaises(ValueError):
            device.provision(args)
        prompt.assert_not_called()
        args.ca_file.write_bytes(marker + b"A" * (3999 - len(marker)))
        port = self.provision_serial(STATUS)
        replies = ["[ok] " + message + "\n" for message in (
            "network name saved", "password saved", "bridge origin saved",
            "trust anchor saved", "device credential saved")]
        with patch.object(device, "receive", side_effect=replies):
            device.provision(args)
        self.assertEqual(len(port.write.call_args_list), 7)
        ca_command = port.write.call_args_list[-2].args[0]
        self.assertEqual(len(ca_command), 5336)  # Max PEM base64 + "ca " + newline.
        self.assertLessEqual(len(ca_command), 8192)


    def test_raw_origin_command_injection_is_rejected_before_prompt_or_serial(self):
        serial_factory = Mock(side_effect=AssertionError("must not create serial transport"))
        prompt = Mock(side_effect=AssertionError("must not prompt for credentials"))
        token_reader = Mock(side_effect=AssertionError("must not read credentials"))
        serial_module = types.SimpleNamespace(Serial=serial_factory)
        args = argparse.Namespace(approved_device_write=True, hardware_confirmed=True,
                                  port="/dev/fixture-not-a-device", token_file=self.root / "not-read.token",
                                  ca_file=self.root / "not-read.crt")
        with (patch.dict(sys.modules, {"serial": serial_module}),
              patch.object(device, "hidden_input", prompt),
              patch.object(device, "private_token", token_reader)):
            for origin in ("https://voice.example.test\nresume", "https://voice.example.test\rresume",
                           "https://voice.example.test\t", "\nhttps://voice.example.test",
                           "https://voice.example.test/\nresume", "https://voice.example.test\0"):
                args.bridge_url = origin
                with self.subTest(origin=repr(origin)), self.assertRaises(ValueError):
                    device.provision(args)
        prompt.assert_not_called()
        serial_factory.assert_not_called()
        token_reader.assert_not_called()

    def test_firmware_dns_origin_boundaries(self):
        for origin in ("http://voice.example.test", "HTTPS://voice.example.test", "https://127.0.0.1",
                       "https://[::1]", "https://user@voice.example.test", "https://voice.example.test:0",
                       "https://voice.example.test:65536", "https://voice.example.test:",
                       "https://voice.example.test?", "https://voice.example.test#", "https://voice.example.test/api",
                       "https://voice..example.test", "https://-voice.example.test", "https://voice-.example.test",
                       "https://voice.example.test.", "https://" + "a" * 64 + ".test",
                       "https://" + ".".join(["a" * 63] * 4), "https://éxample.test"):
            with self.subTest(invalid=origin), self.assertRaises(ValueError):
                device.validate_bridge_origin(origin)


if __name__ == "__main__":
    unittest.main()
