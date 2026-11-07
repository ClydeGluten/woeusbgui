import os
import time
import tkinter as tk
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from woeusb_gui.app import Application
from woeusb_gui.devices import ValidationError
from tests.test_devices import discover, usb


@unittest.skipUnless(os.environ.get("DISPLAY"), "Run with xvfb-run for GUI tests")
class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.writer = Mock()
        with patch.object(Application, "_dependencies", return_value=""):
            self.app = Application(self.root, scanner=lambda: [], writer=self.writer)
        self.root.update_idletasks()
        self.drive = discover(usb())[0]

    def tearDown(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()

    def select(self):
        self.app._update_drives([self.drive])
        self.app.tree.selection_set(self.drive.path)
        self.app._select()

    def pump(self, seconds=0.2):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def test_never_autoselects_first_drive(self):
        self.app._update_drives([self.drive])
        self.assertIsNone(self.app.selected)
        self.assertIn("disabled", self.app.write_button.state())

    def test_requires_both_iso_and_selection(self):
        self.select()
        self.assertIn("disabled", self.app.write_button.state())
        self.app.source.set("/tmp/Windows.iso")
        self.assertNotIn("disabled", self.app.write_button.state())

    def test_selection_survives_refresh_but_not_device_replacement(self):
        self.select()
        self.app._update_drives([self.drive])
        self.assertEqual(self.app.selected, self.drive)
        self.app._update_drives([replace(self.drive, diskseq="42")])
        self.assertIsNone(self.app.selected)
        self.assertEqual(self.app.tree.selection(), ())

    def test_disappearing_drive_clears_selection(self):
        self.select()
        self.app._update_drives([])
        self.assertIsNone(self.app.selected)
        self.assertIn("No eligible USB", self.app.drive_hint.get())

    @patch("woeusb_gui.app.make_request", return_value={})
    def test_cancelled_confirmation_never_calls_writer(self, _request):
        self.select()
        with patch.object(self.app, "confirm_erase", return_value=False):
            self.app.start_write()
            self.pump()
        self.writer.assert_not_called()
        self.assertFalse(self.app.busy)

    @patch("woeusb_gui.app.make_request", return_value={"source": {"path": "/tmp/Windows.iso"}})
    def test_confirmed_write_streams_log_and_restores_controls(self, _request):
        self.select()
        self.app.source.set("/tmp/Windows.iso")

        def fake_writer(request, emit):
            emit("log", "Copying files…\n")
            emit("finished", 0)

        self.app.writer = fake_writer
        with patch.object(self.app, "confirm_erase", return_value=True):
            self.app.start_write()
            self.assertTrue(self.app.confirming)
            self.assertIn("disabled", self.app.browse.state())
            self.pump()
        self.assertFalse(self.app.busy)
        self.assertIn("Copying files", self.app.log.get("1.0", "end"))
        self.assertIn("completed", self.app.log.get("1.0", "end"))
        self.assertNotIn("disabled", self.app.browse.state())

    def test_demo_cannot_write(self):
        self.app.demo = True
        self.select()
        self.app.source.set("/tmp/Windows.iso")
        self.app.start_write()
        self.writer.assert_not_called()
        self.assertIn("disabled", self.app.write_button.state())

    @patch("woeusb_gui.app.messagebox.showerror")
    @patch("woeusb_gui.app.make_request", side_effect=ValidationError("ISO changed"))
    def test_preflight_failure_restores_controls_without_writing(self, _request, error):
        self.select()
        self.app.start_write()
        self.pump()
        self.assertFalse(self.app.confirming)
        self.assertFalse(self.app.busy)
        self.writer.assert_not_called()
        error.assert_called_once()
        self.assertNotIn("disabled", self.app.browse.state())

    def test_scan_failure_removes_stale_drive(self):
        self.select()
        self.app.events.put(("scan_error", "lsblk failed"))
        self.app._drain()
        self.assertIsNone(self.app.selected)
        self.assertEqual(self.app.drive_hint.get(), "lsblk failed")

    def test_authentication_cancel_is_visible(self):
        self.app.busy = True
        self.app.events.put(("finished", 126))
        self.app._drain()
        self.assertFalse(self.app.busy)
        self.assertIn("Authentication cancelled", self.app.status.get())

    def test_error_status_is_visible(self):
        self.app.events.put(("finished", 1))
        self.app._drain()
        self.assertIn("failed", self.app.status.get())

    def test_real_confirmation_dialog_requires_exact_device_name(self):
        def fill():
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))
            from tkinter import ttk
            entry = next(child for child in dialog.winfo_children() if isinstance(child, ttk.Entry))
            buttons = [child for frame in dialog.winfo_children() for child in frame.winfo_children()
                       if isinstance(child, ttk.Button) and child.cget("text") == "Erase & create USB"]
            self.assertIn("disabled", buttons[0].state())
            entry.insert(0, "sdb")
            self.assertNotIn("disabled", buttons[0].state())
            buttons[0].invoke()

        self.root.after(50, fill)
        self.assertTrue(self.app.confirm_erase(self.drive))
