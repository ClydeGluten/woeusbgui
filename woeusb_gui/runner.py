"""Run the helper off the GUI thread and stream its output."""

from __future__ import annotations

import codecs
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .devices import system_tool


def helper_command(request: dict) -> list[str]:
    helper = str(Path(__file__).resolve().with_name("helper.py"))
    return [system_tool("pkexec"), "--disable-internal-agent", system_tool("python3"),
            "-I", "-u", helper, json.dumps(request, separators=(",", ":"))]


def write_usb(request: dict, emit: Callable[[str, object], None]) -> None:
    try:
        with subprocess.Popen(
            helper_command(request), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ) as process:
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while chunk := os.read(process.stdout.fileno(), 4096):
                emit("log", decoder.decode(chunk))
            emit("log", decoder.decode(b"", final=True))
            code = process.wait()
        emit("finished", code)
    except (OSError, ValueError) as exc:
        emit("log", f"\nCould not start WoeUSB: {exc}\n")
        emit("finished", 1)
