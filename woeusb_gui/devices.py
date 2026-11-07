"""Read-only device discovery and write-request validation."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
COLUMNS = "NAME,TYPE,TRAN,RO,SIZE,MODEL,SERIAL,WWN,MAJ:MIN,MOUNTPOINTS"
REMOVABLE_MOUNTS = ("/media/", "/run/media/", "/mnt/")


class ValidationError(ValueError):
    """A request cannot safely proceed."""


def system_tool(name: str) -> str:
    path = shutil.which(name, path=SYSTEM_PATH)
    if not path:
        raise ValidationError(f"{name} is missing. See the installation steps in README.md.")
    return str(Path(path).absolute())


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


@dataclass(frozen=True)
class Drive:
    path: str
    size: int
    model: str
    serial: str
    wwn: str
    major_minor: str
    diskseq: str
    block_ids: tuple[str, ...]
    mounts: tuple[str, ...]

    @property
    def identity(self) -> dict:
        return {key: getattr(self, key) for key in (
            "path", "size", "model", "serial", "wwn", "major_minor", "diskseq"
        )}

    @property
    def label(self) -> str:
        return f"{self.model}  ·  {human_size(self.size)}  ·  {self.path}"


def read_diskseq(path: str) -> str:
    try:
        return (Path("/sys/class/block") / Path(path).name / "diskseq").read_text().strip()
    except OSError:
        return ""


def _walk(node: dict):
    yield node
    for child in node.get("children") or []:
        yield from _walk(child)


def _text(value) -> str:
    return str(value or "").strip()


def _flag(value) -> bool:
    return value is True or value == 1 or value == "1"


def parse_devices(payload: dict, diskseq_reader: Callable[[str], str] = read_diskseq) -> list[Drive]:
    """Only whole, writable USB disks with no system mounts or active mappings."""
    if not isinstance(payload, dict) or not isinstance(payload.get("blockdevices"), list):
        raise ValidationError("lsblk returned an invalid device list.")
    found = {}
    for root in payload["blockdevices"]:
        for node in _walk(root):
            if node.get("type") != "disk" or node.get("tran") != "usb":
                continue
            children = list(_walk(node))
            if any(_flag(child.get("ro")) for child in children):
                continue
            if any(child.get("type") != "part" for child in children[1:]):
                continue  # Active LVM, encryption, RAID, etc. are never write targets.
            mounts = tuple(sorted({
                mount for child in children for mount in (child.get("mountpoints") or [])
                if mount
            }))
            # Also excludes swap and USB disks hosting /, /home, /boot, etc.
            if any(not mount.startswith(REMOVABLE_MOUNTS) for mount in mounts):
                continue
            path = _text(node.get("name"))
            size = int(node.get("size") or 0)
            major_minor = _text(node.get("maj:min"))
            if not path.startswith("/dev/") or size <= 0 or not major_minor:
                continue
            serial, wwn = _text(node.get("serial")), _text(node.get("wwn"))
            diskseq = diskseq_reader(path)
            if not (serial or wwn or diskseq):
                continue  # Cannot identify this drive across an authentication delay.
            found[path] = Drive(
                path, size, _text(node.get("model")) or "USB drive", serial, wwn,
                major_minor, diskseq,
                tuple(_text(child.get("maj:min")) for child in children), mounts,
            )
    return sorted(found.values(), key=lambda drive: drive.path)


def scan_drives() -> list[Drive]:
    try:
        result = subprocess.run(
            [system_tool("lsblk"), "--json", "--bytes", "--paths", "--tree", "--output", COLUMNS],
            capture_output=True, text=True, check=True, timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        )
        return parse_devices(json.loads(result.stdout))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError) as exc:
        raise ValidationError(f"Could not scan USB drives: {exc}") from exc


def current_drive(expected: dict, drives: list[Drive]) -> Drive:
    for drive in drives:
        if drive.identity == expected:
            return drive
    raise ValidationError("The selected drive changed or was unplugged. Refresh and select it again.")


@dataclass(frozen=True)
class Source:
    path: str
    device: int
    inode: int
    size: int
    modified_ns: int


def inspect_source(path: str) -> Source:
    if not path.strip():
        raise ValidationError("Choose a Windows ISO first.")
    source = Path(path).expanduser().resolve()
    try:
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or source.suffix.lower() != ".iso":
            raise ValidationError("Choose a regular .iso file.")
        if info.st_size == 0:
            raise ValidationError("The selected ISO is empty.")
        with source.open("rb") as stream:
            stream.read(1)
    except OSError as exc:
        raise ValidationError(f"Cannot read the ISO: {exc}") from exc
    return Source(str(source), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def validate_source_on_drive(source: Source, drive: Drive) -> None:
    source_id = f"{os.major(source.device)}:{os.minor(source.device)}"
    on_mount = any(Path(source.path).is_relative_to(Path(mount)) for mount in drive.mounts)
    if source_id in drive.block_ids or on_mount:
        raise ValidationError("The ISO is stored on the target USB drive. Move it to your computer first.")
    if source.size >= drive.size:
        raise ValidationError("The USB drive must be larger than the ISO. Choose a larger drive.")


def make_request(source_path: str, expected: dict, filesystem: str) -> dict:
    if filesystem not in ("NTFS", "FAT"):
        raise ValidationError("Choose NTFS or FAT32.")
    drive = current_drive(expected, scan_drives())
    source = inspect_source(source_path)
    validate_source_on_drive(source, drive)
    return {"drive": drive.identity, "source": asdict(source), "filesystem": filesystem}


def validate_request(request: dict) -> tuple[Source, Drive]:
    if request.get("filesystem") not in ("NTFS", "FAT"):
        raise ValidationError("Unsupported target filesystem.")
    source = inspect_source(request["source"]["path"])
    if asdict(source) != request["source"]:
        raise ValidationError("The ISO changed after confirmation. Select it again.")
    drive = current_drive(request["drive"], scan_drives())
    validate_source_on_drive(source, drive)
    return source, drive
