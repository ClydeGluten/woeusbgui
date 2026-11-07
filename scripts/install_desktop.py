#!/usr/bin/env python3
"""Install an application-menu shortcut pointing at this source checkout."""

import os
from pathlib import Path


def desktop_quote(value: str) -> str:
    # Desktop Entry Exec has its own escaping rules, including literal % codes.
    value = value.replace("%", "%%").replace("\\", "\\\\\\\\")
    for character in ('"', '`', '$'):
        value = value.replace(character, "\\\\" + character)
    return '"' + value + '"'


def main():
    project = Path(__file__).resolve().parent.parent
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    target = data_home / "applications/woeusb-gui.desktop"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[Desktop Entry]\nType=Application\nName=WoeUSB GUI\n"
        "Comment=Create a Windows installer USB\n"
        f"Exec=/bin/sh {desktop_quote(str(project / 'run.sh'))}\n"
        "Icon=drive-removable-media-usb\nTerminal=false\n"
        "Categories=System;\nKeywords=Windows;USB;ISO;WoeUSB;\n",
        encoding="utf-8",
    )
    print(f"Installed {target}")


if __name__ == "__main__":
    main()
