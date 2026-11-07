"""Tk desktop interface. All discovery and writing happens in worker threads."""

from __future__ import annotations

import argparse
import os
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .devices import Drive, ValidationError, human_size, make_request, scan_drives, system_tool
from .runner import write_usb

BG = "#101720"
PANEL = "#19232f"
TEXT = "#e6edf5"
MUTED = "#9bafc5"
ACCENT = "#67d8c1"
WARNING = "#f4c174"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\([A-Z])")


class Application:
    def __init__(self, root: tk.Tk, *, demo=False, scanner=scan_drives, writer=write_usb):
        self.root = root
        self.demo = demo
        self.scanner = scanner
        self.writer = writer
        self.events = queue.Queue()
        self.drives: dict[str, Drive] = {}
        self.selected: Drive | None = None
        self.busy = False
        self.confirming = False
        self.scanning = False
        self.source = tk.StringVar()
        self.filesystem = tk.StringVar(value="NTFS")
        self.status = tk.StringVar(value="Choose an ISO and a USB drive to get started.")
        self.drive_hint = tk.StringVar(value="Scanning for USB drives…")
        self.details = tk.StringVar(value="Select a drive to see its details.")
        self.dependencies = self._dependencies()
        root.title("WoeUSB GUI" + (" — Preview" if demo else ""))
        root.geometry("860x820")
        root.minsize(760, 740)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._styles()
        self._build()
        self.source.trace_add("write", lambda *_: self._controls())
        root.after(80, self._drain)
        root.after(150, self._poll_drives)
        if demo:
            self.source.set("/home/demo/Downloads/Windows_11_x64.iso")
            self.status.set("PREVIEW MODE  ·  Writing is disabled; these are example drives.")
        elif self.dependencies:
            self.status.set(self.dependencies)
        self._controls()

    def _dependencies(self) -> str:
        missing = []
        for name in ("woeusb", "lsblk", "pkexec", "python3", "umount"):
            try:
                system_tool(name)
            except ValidationError:
                missing.append(name)
        return f"Missing: {', '.join(missing)}. See README.md to install." if missing else ""

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Sans", 10))
        style.configure("TButton", background="#29394a", foreground=TEXT,
                        borderwidth=0, padding=(14, 9), font=("Sans", 10))
        style.map("TButton", background=[("active", "#364b60"), ("disabled", "#202c38")],
                  foreground=[("disabled", "#738497")])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG,
                        font=("Sans", 11, "bold"))
        style.map("Accent.TButton", background=[("disabled", "#29443f"), ("active", "#8be8d5")],
                  foreground=[("disabled", "#819f98")])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT,
                        insertcolor=TEXT, borderwidth=1, padding=9)
        style.map("TEntry", fieldbackground=[("disabled", PANEL)])
        style.configure("TCombobox", fieldbackground=PANEL, background="#29394a",
                        foreground=TEXT, arrowcolor=TEXT, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=38, borderwidth=0, font=("Sans", 10))
        style.map("Treeview", background=[("selected", "#28544f")], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background="#233140", foreground=MUTED,
                        padding=(8, 8), relief="flat", font=("Sans", 9, "bold"))
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL,
                        borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)

    def _label(self, parent, text=None, *, variable=None, muted=False, size=10, bold=False, **kw):
        return tk.Label(parent, text=text, textvariable=variable, bg=BG,
                        fg=MUTED if muted else TEXT,
                        font=("Sans", size, "bold" if bold else "normal"), anchor="w", **kw)

    def _build(self):
        page = tk.Frame(self.root, bg=BG, padx=28, pady=22)
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(9, weight=1)

        header = tk.Frame(page, bg=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        self._label(header, "WoeUSB", size=27, bold=True).pack(side="left")
        self._label(header, "  /  WINDOWS USB MAKER", muted=True, size=10).pack(side="left", pady=(10, 0))
        self._label(header, f"v{__version__}", muted=True, size=9).pack(side="right", pady=(10, 0))

        self._label(page, "01   Windows installation image", size=12, bold=True).grid(row=1, column=0, sticky="w")
        source_row = tk.Frame(page, bg=BG)
        source_row.grid(row=2, column=0, sticky="ew", pady=(10, 7))
        source_row.columnconfigure(0, weight=1)
        self.source_entry = ttk.Entry(source_row, textvariable=self.source, font=("Sans", 10))
        self.source_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.browse = ttk.Button(source_row, text="Choose ISO…", command=self.choose_iso)
        self.browse.grid(row=0, column=1)
        self._label(page, "Use a Windows installation .iso file saved on your computer.", muted=True,
                    size=9).grid(row=3, column=0, sticky="w", pady=(0, 20))

        drive_header = tk.Frame(page, bg=BG)
        drive_header.grid(row=4, column=0, sticky="ew", pady=(0, 9))
        self._label(drive_header, "02   USB drive", size=12, bold=True).pack(side="left")
        self.refresh_button = ttk.Button(drive_header, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="right")
        self._label(drive_header, "Auto-detects every 3 seconds   ", muted=True, size=9).pack(side="right")

        drive_area = tk.Frame(page, bg=BG)
        drive_area.grid(row=5, column=0, sticky="ew")
        drive_area.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(drive_area, columns=("model", "size", "device", "state"),
                                 show="headings", height=3, selectmode="browse")
        for key, title, width in (("model", "DRIVE", 260), ("size", "CAPACITY", 100),
                                  ("device", "DEVICE", 110), ("state", "VOLUMES", 100)):
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=width, minwidth=80, stretch=key == "model")
        self.tree.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(drive_area, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Button-1>", lambda _: "break" if self.busy or self.confirming else None)
        self.tree.bind("<KeyPress>", lambda _: "break" if self.busy or self.confirming else None)
        self._label(drive_area, variable=self.drive_hint, muted=True, size=9).grid(
            row=1, column=0, sticky="w", pady=(7, 0))
        self._label(drive_area, variable=self.details, muted=True, size=9,
                    wraplength=740, justify="left").grid(row=2, column=0, sticky="w", pady=(5, 0))

        options = tk.Frame(page, bg=BG)
        options.grid(row=6, column=0, sticky="ew", pady=(18, 14))
        self._label(options, "Filesystem", bold=True).pack(side="left", padx=(0, 12))
        self.fs_combo = ttk.Combobox(options, textvariable=self.filesystem,
                                     values=("NTFS", "FAT32"), state="readonly", width=9)
        self.fs_combo.pack(side="left")
        self._label(options, "NTFS supports files larger than 4 GiB.", muted=True, size=9).pack(side="left", padx=12)

        warning = tk.Label(page, text="The selected USB drive will be erased. Back up its files before writing.",
                           bg="#332b20", fg=WARNING, font=("Sans", 10), anchor="w", padx=12, pady=11)
        warning.grid(row=7, column=0, sticky="ew", pady=(0, 14))

        log_header = tk.Frame(page, bg=BG)
        log_header.grid(row=8, column=0, sticky="ew", pady=(0, 7))
        self._label(log_header, "Activity", bold=True).pack(side="left")
        ttk.Button(log_header, text="Save log…", command=self.save_log).pack(side="right")
        log_frame = tk.Frame(page, bg=PANEL)
        log_frame.grid(row=9, column=0, sticky="nsew")
        self.log = tk.Text(log_frame, height=5, wrap="word", bg=PANEL, fg=MUTED,
                           insertbackground=TEXT, relief="flat", padx=12, pady=10,
                           font=("Monospace", 9), state="disabled", highlightthickness=0)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)
        self._append("Ready. Select an ISO and a USB drive.\nAdministrator authentication is requested only when writing.\n")

        self.progress = ttk.Progressbar(page, mode="indeterminate")
        self.progress.grid(row=10, column=0, sticky="ew", pady=(12, 9))
        footer = tk.Frame(page, bg=BG)
        footer.grid(row=11, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self._label(footer, variable=self.status, muted=True, size=9,
                    wraplength=500, justify="left").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.write_button = ttk.Button(footer, text="Create Windows USB", style="Accent.TButton", command=self.start_write)
        self.write_button.grid(row=0, column=1, sticky="e")

    def choose_iso(self):
        path = filedialog.askopenfilename(parent=self.root, title="Choose Windows installation ISO",
                                          filetypes=[("ISO disk images", "*.iso *.ISO"), ("All files", "*")])
        if path:
            self.source.set(path)

    def _poll_drives(self):
        self.refresh()
        self.root.after(3000, self._poll_drives)

    def refresh(self):
        if self.scanning or self.busy or self.confirming:
            return
        self.scanning = True
        self.refresh_button.configure(state="disabled")

        def scan():
            try:
                if self.demo:
                    drives = [
                        Drive("/dev/sdb", 32_000_000_000, "SanDisk Ultra", "DEMO-001", "", "8:16", "1", ("8:16",), ()),
                        Drive("/dev/sdc", 64_000_000_000, "Kingston DataTraveler", "DEMO-002", "", "8:32", "2", ("8:32",), ("/media/demo/KINGSTON",)),
                    ]
                else:
                    drives = self.scanner()
                self.events.put(("drives", drives))
            except Exception as exc:
                self.events.put(("scan_error", str(exc)))

        threading.Thread(target=scan, daemon=True).start()

    def _update_drives(self, drives):
        old = self.selected
        self.drives = {drive.path: drive for drive in drives}
        self.selected = None
        for item in self.tree.get_children():
            if item not in self.drives:
                self.tree.delete(item)
        for drive in drives:
            values = (drive.model, human_size(drive.size), drive.path,
                      "Mounted" if drive.mounts else "Unmounted")
            if self.tree.exists(drive.path):
                self.tree.item(drive.path, values=values)
            else:
                self.tree.insert("", "end", iid=drive.path, values=values)
            if old and old.identity == drive.identity:
                self.selected = drive
        if self.selected is None:
            self.tree.selection_remove(self.tree.selection())
            if old:
                self.status.set("Drive removed or changed. Select a USB drive again.")
        else:
            self.tree.selection_set(self.selected.path)
        count = len(drives)
        self.drive_hint.set(
            f"{count} USB drive{'s' if count != 1 else ''} available. Internal and system disks are hidden."
            if count else "No eligible USB drives. Plug in a thumb drive; detection is automatic."
        )
        self._selection_details()
        self._controls()

    def _select(self, _event=None):
        if self.busy or self.confirming:
            return
        selection = self.tree.selection()
        self.selected = self.drives.get(selection[0]) if selection else None
        self._selection_details()
        self._controls()

    def _selection_details(self):
        if self.selected:
            drive = self.selected
            mounts = ", ".join(drive.mounts) or "none"
            self.details.set(f"Serial: {drive.serial or 'unavailable'}  ·  Mounted volumes: {mounts}")
        else:
            self.details.set("Select a drive to see its details.")

    def _controls(self):
        locked = self.busy or self.confirming
        self.source_entry.configure(state="disabled" if locked or self.demo else "normal")
        self.browse.configure(state="disabled" if locked or self.demo else "normal")
        self.fs_combo.configure(state="disabled" if locked else "readonly")
        self.refresh_button.configure(state="disabled" if locked or self.scanning else "normal")
        ready = bool(self.selected and self.source.get().strip() and not self.dependencies)
        self.write_button.configure(state="normal" if ready and not locked and not self.demo else "disabled")

    def confirm_erase(self, drive: Drive) -> bool:
        dialog = tk.Toplevel(self.root)
        dialog.title("Confirm USB erase")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.configure(bg=BG, padx=24, pady=22)
        self._label(dialog, "Erase this USB drive?", size=18, bold=True).pack(anchor="w", pady=(0, 14))
        self._label(dialog,
                    f"{drive.model} · {human_size(drive.size)} · {drive.path}\n"
                    f"Serial: {drive.serial or 'unavailable'}\n\n"
                    "All partitions and files on this drive will be erased.\n"
                    "Any mounted USB volumes will be unmounted automatically.\n"
                    f"Type {Path(drive.path).name} below to confirm.",
                    wraplength=480, justify="left").pack(anchor="w")
        typed = tk.StringVar()
        entry = ttk.Entry(dialog, textvariable=typed, width=35)
        entry.pack(fill="x", pady=16)
        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x")
        accepted = False

        def accept():
            nonlocal accepted
            if typed.get() == Path(drive.path).name:
                accepted = True
                dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left")
        erase = ttk.Button(buttons, text="Erase & create USB", command=accept,
                            style="Accent.TButton", state="disabled")
        erase.pack(side="right")
        typed.trace_add("write", lambda *_: erase.configure(
            state="normal" if typed.get() == Path(drive.path).name else "disabled"))
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.bind("<Return>", lambda _: accept())
        dialog.grab_set()
        entry.focus_set()
        self.root.wait_window(dialog)
        return accepted

    def start_write(self):
        if self.busy or self.confirming or self.demo or not self.selected:
            return
        drive = self.selected
        self.confirming = True
        source_path = self.source.get()
        filesystem = "FAT" if self.filesystem.get() == "FAT32" else "NTFS"
        self.status.set("Checking the ISO and USB drive…")
        self._controls()

        def prepare():
            try:
                request = make_request(source_path, drive.identity, filesystem)
                self.events.put(("prepared", (request, drive)))
            except Exception as exc:
                self.events.put(("prepare_error", str(exc)))

        threading.Thread(target=prepare, daemon=True).start()

    def _confirm_write(self, request, drive):
        try:
            if not self.confirm_erase(drive):
                self.status.set("Cancelled. No files were changed.")
                return
        finally:
            self.confirming = False
            self._controls()
        self.busy = True
        self._controls()
        self.status.set("Writing… Keep this window open and leave the USB drive connected.")
        self._append(f"\nISO: {request['source']['path']}\nTarget: {drive.label}\n"
                     "Waiting for administrator authentication…\n")
        self.progress.start(15)
        threading.Thread(target=self.writer, args=(request, lambda kind, value: self.events.put((kind, value))),
                         daemon=True).start()

    def _append(self, text):
        text = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self.log.configure(state="normal")
        self.log.insert("end", text)
        if int(self.log.index("end-1c").split(".")[0]) > 6000:
            self.log.delete("1.0", "1001.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        for _ in range(200):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind in ("drives", "scan_error"):
                self.scanning = False
                if not self.busy and not self.confirming:
                    if kind == "drives":
                        self._update_drives(value)
                    else:
                        self._update_drives([])
                        self.drive_hint.set(value)
                self._controls()
            elif kind == "log":
                self._append(value)
            elif kind == "prepared":
                self._confirm_write(*value)
            elif kind == "prepare_error":
                self.confirming = False
                self.status.set(value)
                self._controls()
                messagebox.showerror("Cannot create USB", value, parent=self.root)
            elif kind == "finished":
                self.busy = False
                self.progress.stop()
                self.progress.configure(value=0)
                if value == 0:
                    result = "USB creation completed. Eject the drive before unplugging it."
                elif value in (126, 127):
                    result = "Authentication cancelled or unavailable. Check the activity log."
                else:
                    result = f"USB creation failed (exit {value}). Check the activity log before retrying."
                self.status.set(result)
                self._append(f"\n{result}\n")
                self._controls()
                self.refresh()
        self.root.after(80, self._drain)

    def save_log(self):
        path = filedialog.asksaveasfilename(parent=self.root, title="Save activity log",
                                           defaultextension=".txt", initialfile="woeusb-log.txt")
        if path:
            try:
                Path(path).write_text(self.log.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Cannot save log", str(exc), parent=self.root)

    def close(self):
        if self.busy or self.confirming:
            messagebox.showinfo("Operation in progress", "Wait for the operation to finish before closing this window.", parent=self.root)
            return
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="A small desktop front end for WoeUSB.")
    parser.add_argument("--demo", action="store_true", help="Preview the interface with example drives; writing is disabled")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.exit(1, "Run the GUI as your normal desktop user. It asks for administrator access when needed.\n")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        parser.exit(1, f"A graphical desktop and Tk are required: {exc}\n")
    Application(root, demo=args.demo)
    root.mainloop()
    return 0
