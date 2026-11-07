import json
import subprocess
import sys
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from woeusb_gui.devices import ValidationError
from woeusb_gui.helper import build_command, perform_write
from woeusb_gui.runner import helper_command, write_usb
from tests.test_devices import discover, usb


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.request = {"source": {"path": "/tmp/Windows ' ; $(oops).iso"},
                        "drive": {"path": "/dev/sdb"}, "filesystem": "NTFS"}

    def test_command_keeps_paths_as_literal_arguments(self):
        command = build_command("/usr/bin/woeusb", self.request)
        self.assertEqual(command, ["/usr/bin/woeusb", "--device", self.request["source"]["path"],
                                   "/dev/sdb", "--target-filesystem", "NTFS", "--no-color"])

    @patch("woeusb_gui.runner.system_tool", side_effect=lambda name: "/usr/bin/" + name)
    def test_helper_runs_isolated_and_request_is_one_json_argument(self, _tool):
        command = helper_command(self.request)
        self.assertEqual(command[:5], ["/usr/bin/pkexec", "--disable-internal-agent", "/usr/bin/python3", "-I", "-u"])
        self.assertEqual(json.loads(command[-1]), self.request)

    def test_streamed_output_and_exit_status(self):
        events = []
        command = [sys.executable, "-c", "print('Copying…', flush=True); raise SystemExit(3)"]
        with patch("woeusb_gui.runner.helper_command", return_value=command):
            write_usb(self.request, lambda *event: events.append(event))
        self.assertIn("Copying…", "".join(value for kind, value in events if kind == "log"))
        self.assertEqual(events[-1], ("finished", 3))

    def test_authentication_cancel_reported(self):
        events = []
        with patch("woeusb_gui.runner.helper_command", return_value=[sys.executable, "-c", "raise SystemExit(126)"]):
            write_usb(self.request, lambda *event: events.append(event))
        self.assertEqual(events[-1], ("finished", 126))

    def test_spawn_failure_reported(self):
        events = []
        with patch("woeusb_gui.runner.helper_command", return_value=["/nonexistent/command"]):
            write_usb(self.request, lambda *event: events.append(event))
        self.assertEqual(events[-1], ("finished", 1))

    @patch("woeusb_gui.helper.os.geteuid", return_value=1000)
    def test_helper_refuses_unprivileged_write(self, _uid):
        with self.assertRaises(ValidationError):
            perform_write(self.request)

    @patch("woeusb_gui.helper.run_in_terminal")
    @patch("woeusb_gui.helper.validate_request", side_effect=ValidationError("Drive changed"))
    @patch("woeusb_gui.helper.trusted_executable", return_value="/usr/bin/woeusb")
    @patch("woeusb_gui.helper.os.geteuid", return_value=0)
    def test_post_authentication_validation_failure_never_writes(self, _uid, _tool, _validate, run):
        with self.assertRaises(ValidationError):
            perform_write(self.request)
        run.assert_not_called()

    @patch("woeusb_gui.helper.run_in_terminal", return_value=0)
    @patch("woeusb_gui.helper.subprocess.run", return_value=Mock(returncode=0))
    @patch("woeusb_gui.helper.validate_request")
    @patch("woeusb_gui.helper.trusted_executable", side_effect=lambda name: "/usr/bin/" + name)
    @patch("woeusb_gui.helper.os.geteuid", return_value=0)
    def test_unmount_then_recheck_before_write(self, _uid, _tool, validate, unmount, run):
        drive = discover(usb())[0]
        validate.side_effect = [(None, drive), (None, drive), (None, replace(drive, mounts=()))]
        self.assertEqual(perform_write(self.request), 0)
        self.assertEqual(unmount.call_args.args[0], ["/usr/bin/umount", "--", "/media/user/USB"])
        self.assertEqual(validate.call_count, 3)
        run.assert_called_once()

    @patch("woeusb_gui.helper.run_in_terminal")
    @patch("woeusb_gui.helper.subprocess.run", return_value=Mock(returncode=1, stderr="busy"))
    @patch("woeusb_gui.helper.validate_request")
    @patch("woeusb_gui.helper.trusted_executable", side_effect=lambda name: "/usr/bin/" + name)
    @patch("woeusb_gui.helper.os.geteuid", return_value=0)
    def test_busy_volume_never_writes(self, _uid, _tool, validate, _unmount, run):
        validate.return_value = (None, discover(usb())[0])
        with self.assertRaisesRegex(ValidationError, "Close files"):
            perform_write(self.request)
        run.assert_not_called()

    @patch("woeusb_gui.helper.run_in_terminal")
    @patch("woeusb_gui.helper.subprocess.run", return_value=Mock(returncode=0))
    @patch("woeusb_gui.helper.validate_request")
    @patch("woeusb_gui.helper.trusted_executable", side_effect=lambda name: "/usr/bin/" + name)
    @patch("woeusb_gui.helper.os.geteuid", return_value=0)
    def test_automount_race_never_writes(self, _uid, _tool, validate, _unmount, run):
        validate.return_value = (None, discover(usb())[0])
        with self.assertRaisesRegex(ValidationError, "mounted again"):
            perform_write(self.request)
        run.assert_not_called()

    def test_real_pty_supports_stty_and_upstream_error_prompts(self):
        # Only this harmless Python subprocess is executed; no WoeUSB or disks.
        child = (
            "import os, sys; print('TTY', os.isatty(0), flush=True); "
            "print('Read command output (Y/n)?', end='', flush=True); "
            "assert input() == 'y'; print('useful error detail', flush=True); "
            "print('Press ENTER to continue', flush=True); input(); sys.exit(7)"
        )
        harness = "from woeusb_gui.helper import run_in_terminal; import sys; sys.exit(run_in_terminal(sys.argv[1:]))"
        result = subprocess.run([sys.executable, "-c", harness, sys.executable, "-c", child],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("TTY True", result.stdout)
        self.assertIn("useful error detail", result.stdout)
