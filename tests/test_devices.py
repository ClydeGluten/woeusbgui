import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from woeusb_gui.devices import (
    Source, ValidationError, current_drive, inspect_source, make_request,
    parse_devices, scan_drives, validate_request, validate_source_on_drive,
)


def usb(**changes):
    node = dict(name="/dev/sdb", type="disk", tran="usb", ro=False, size=32_000_000_000,
                model="Test USB", serial="1234", wwn=None, mountpoints=[],
                children=[dict(name="/dev/sdb1", type="part", ro=False,
                               mountpoints=["/media/user/USB"], **{"maj:min": "8:17"})],
                **{"maj:min": "8:16"})
    node.update(changes)
    return node


def discover(*nodes, seq="41"):
    return parse_devices({"blockdevices": list(nodes)}, lambda _: seq)


class DiscoveryTests(unittest.TestCase):
    def test_usb_disk_with_mounted_partition_is_one_target(self):
        drives = discover(usb())
        self.assertEqual(len(drives), 1)
        self.assertEqual(drives[0].mounts, ("/media/user/USB",))
        self.assertEqual(drives[0].block_ids, ("8:16", "8:17"))

    def test_internal_removable_loop_and_readonly_devices_excluded(self):
        for changes in ({"tran": "nvme"}, {"tran": "sata", "rm": True},
                        {"tran": None}, {"type": "loop"}, {"ro": True}, {"ro": "1"}):
            with self.subTest(changes=changes):
                self.assertEqual(discover(usb(**changes)), [])

    def test_usb_fixed_disks_are_detected_even_without_removable_flag(self):
        self.assertEqual(len(discover(usb(rm=False))), 1)

    def test_system_mounts_and_swap_are_excluded(self):
        for mount in ("/", "/home", "/boot/efi", "/var", "[SWAP]", "/srv", "/custom"):
            node = usb()
            node["children"][0]["mountpoints"] = [mount]
            with self.subTest(mount=mount):
                self.assertEqual(discover(node), [])

    def test_nested_system_mounts_and_active_mappings_excluded(self):
        node = usb()
        node["children"][0]["children"] = [dict(type="crypt", mountpoints=[])]
        self.assertEqual(discover(node), [])
        node["children"][0]["children"] = [dict(type="part", mountpoints=["/"])]
        self.assertEqual(discover(node), [])

    def test_readonly_partition_excludes_disk(self):
        node = usb()
        node["children"][0]["ro"] = True
        self.assertEqual(discover(node), [])

    def test_no_stable_identity_is_not_eligible(self):
        self.assertEqual(discover(usb(serial="", wwn=""), seq=""), [])
        self.assertEqual(len(discover(usb(serial="", wwn=""))), 1)

    def test_invalid_disk_properties_are_excluded(self):
        for changes in ({"size": 0}, {"name": "/tmp/sdb"}, {"maj:min": ""}):
            self.assertEqual(discover(usb(**changes)), [])

    def test_invalid_response_fails_closed(self):
        for payload in ({}, {"blockdevices": None}, []):
            with self.assertRaises(ValidationError):
                parse_devices(payload)

    def test_drive_swap_at_same_path_is_rejected(self):
        original = discover(usb())[0]
        for changed in (replace(original, serial="different"), replace(original, diskseq="42"),
                        replace(original, size=64_000_000_000)):
            with self.assertRaises(ValidationError):
                current_drive(original.identity, [changed])

    def test_mount_changes_preserve_identity(self):
        original = discover(usb())[0]
        changed = replace(original, mounts=())
        self.assertEqual(current_drive(original.identity, [changed]), changed)

    @patch("woeusb_gui.devices.subprocess.run")
    def test_scan_failure_does_not_reuse_old_results(self, run):
        import subprocess
        run.side_effect = subprocess.TimeoutExpired("lsblk", 10)
        with self.assertRaises(ValidationError):
            scan_drives()


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.iso = Path(self.temp.name) / "Windows 'test'; $(anything).iso"
        self.iso.write_bytes(b"test image, not bootable")
        self.drive = discover(usb())[0]

    def test_source_path_is_resolved_and_file_identity_captured(self):
        source = inspect_source(str(self.iso))
        self.assertEqual(source.path, str(self.iso.resolve()))
        self.assertEqual(source.size, self.iso.stat().st_size)

    def test_missing_empty_directory_and_non_iso_are_rejected(self):
        empty = Path(self.temp.name) / "empty.iso"
        empty.touch()
        other = Path(self.temp.name) / "other.txt"
        other.write_text("x")
        for path in ("", str(empty), str(other), self.temp.name, "/does/not/exist.iso"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                inspect_source(path)

    def test_source_on_target_partition_is_rejected(self):
        source = Source("/elsewhere/windows.iso", os.makedev(8, 17), 1, 100, 1)
        with self.assertRaisesRegex(ValidationError, "stored on the target"):
            validate_source_on_drive(source, self.drive)

    def test_source_on_target_mount_is_rejected_even_with_virtual_device_id(self):
        source = Source("/media/user/USB/Windows.iso", os.makedev(0, 80), 1, 100, 1)
        with self.assertRaises(ValidationError):
            validate_source_on_drive(source, self.drive)

    def test_small_drive_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "larger"):
            validate_source_on_drive(inspect_source(str(self.iso)), replace(self.drive, size=1))

    def test_request_roundtrip_and_file_change(self):
        with patch("woeusb_gui.devices.scan_drives", return_value=[self.drive]):
            request = make_request(str(self.iso), self.drive.identity, "NTFS")
            source, drive = validate_request(request)
            self.assertEqual(asdict(source), request["source"])
            self.assertEqual(drive, self.drive)
            self.iso.write_bytes(b"replaced")
            with self.assertRaisesRegex(ValidationError, "ISO changed"):
                validate_request(request)

    def test_drive_removed_after_confirmation_is_rejected(self):
        with patch("woeusb_gui.devices.scan_drives", return_value=[self.drive]):
            request = make_request(str(self.iso), self.drive.identity, "FAT")
        with patch("woeusb_gui.devices.scan_drives", return_value=[]):
            with self.assertRaisesRegex(ValidationError, "unplugged"):
                validate_request(request)

    def test_unknown_filesystem_is_rejected(self):
        with self.assertRaises(ValidationError):
            make_request(str(self.iso), self.drive.identity, "ext4")
        with self.assertRaises(ValidationError):
            validate_request({"filesystem": "ext4"})
