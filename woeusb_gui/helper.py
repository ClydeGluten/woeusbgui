"""Per-write privileged helper. No persistent service or custom polkit policy."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import re
import struct
import subprocess
import sys
import termios
from pathlib import Path

# Invoked by absolute filename with python -I: import only our adjacent package.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from woeusb_gui.devices import (  # noqa: E402
    SYSTEM_PATH, ValidationError, system_tool, validate_request,
)


def trusted_executable(name: str) -> str:
    executable = Path(system_tool(name)).resolve()
    for path in (executable, *executable.parents):
        info = path.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValidationError(f"Install {name} system-wide in a root-owned location first.")
    return str(executable)


def build_command(woeusb: str, request: dict) -> list[str]:
    return [woeusb, "--device", request["source"]["path"], request["drive"]["path"],
            "--target-filesystem", request["filesystem"], "--no-color"]


def run_in_terminal(command: list[str]) -> int:
    """WoeUSB uses stty and can prompt to show errors; give it a real PTY."""
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        settings = termios.tcgetattr(slave)
        settings[3] &= ~termios.ECHO
        termios.tcsetattr(slave, termios.TCSANOW, settings)
        process = subprocess.Popen(
            command, stdin=slave, stdout=slave, stderr=slave,
            env={"PATH": SYSTEM_PATH, "LC_ALL": "C.UTF-8", "TERM": "dumb", "HOME": "/root"},
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        tail = b""
        output_open = True
        while True:
            try:
                data = os.read(master, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:  # Linux PTY EOF.
                    break
                raise
            if not data:
                break
            if output_open:
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except BrokenPipeError:
                    output_open = False  # Let WoeUSB finish even if the GUI disappears.
            tail = (tail + data)[-4096:]
            # These two upstream prompts only reveal error output; never answer
            # arbitrary prompts or supply an automatic "yes" to a write decision.
            if re.search(rb"Read command output \(Y/n\)\?", tail):
                os.write(master, b"y\n")
                tail = b""
            elif b"Press ENTER to continue" in tail:
                os.write(master, b"\n")
                tail = b""
        return process.wait()
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if process is not None and process.poll() is None:
            process.wait()


def perform_write(request: dict) -> int:
    if os.geteuid() != 0:
        raise ValidationError("Writing requires administrator authentication through pkexec.")
    os.environ["PATH"] = SYSTEM_PATH
    woeusb = trusted_executable("woeusb")
    _, drive = validate_request(request)  # Repeat all checks AFTER the password dialog.
    if drive.mounts:
        umount = trusted_executable("umount")
        for mount in sorted(drive.mounts, key=len, reverse=True):
            _, current = validate_request(request)
            if mount not in current.mounts:
                continue
            print(f"Unmounting {mount}…", flush=True)
            result = subprocess.run(
                [umount, "--", mount], capture_output=True, text=True, timeout=30,
            )
            if result.returncode:
                raise ValidationError(
                    f"Could not unmount {mount}. Close files on the USB and try again.\n"
                    f"{result.stderr.strip()}"
                )
    _, drive = validate_request(request)
    if drive.mounts:
        raise ValidationError("The drive was mounted again. Unmount it and retry.")
    print(f"Writing {request['filesystem']} installer to {drive.path}…", flush=True)
    return run_in_terminal(build_command(woeusb, request))


def main() -> int:
    try:
        if len(sys.argv) != 2:
            raise ValidationError("Launch the desktop app to prepare a write request.")
        return perform_write(json.loads(sys.argv[1]))
    except (ValidationError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
