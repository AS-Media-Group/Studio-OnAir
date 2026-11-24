#!/usr/bin/env python3
"""On-Air Bridge: the window. Pick the desk, learn the faders, tick the relays, press Start.

    python onair_gui.py              normal
    python onair_gui.py --minimized  what "Start with Windows" uses

Settings live in %APPDATA%\\OnAirBridge\\onair.toml (the same file the command-line bridge reads).
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import onair_bridge as ob

try:  # tray icon; the window still works without them
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    pystray = None

APP_NAME = "OnAirBridge"
LOCK_PORT = 47653  # a second copy would fight the first one for the relay's COM port
DRIVER_URL = "https://www.deshide.com/News-detail_CP210x_VCP_Drivers.html"
RED, GREY, GREEN, AMBER = "#d32f2f", "#9e9e9e", "#2e7d32", "#ef6c00"


# --------------------------------------------------------------------------- files and Windows autostart


def data_dir() -> Path:
    base = os.environ.get("APPDATA")
    d = Path(base) / APP_NAME if base else Path.home() / ".onair-bridge"
    d.mkdir(parents=True, exist_ok=True)
    return d


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable).with_name("pythonw.exe")
    return f'"{exe if exe.exists() else sys.executable}" "{Path(__file__).resolve()}" --minimized'


def autostart_supported() -> bool:
    return sys.platform == "win32"


def get_autostart() -> bool:
    if not autostart_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except FileNotFoundError:
        return False


def set_autostart(on: bool) -> None:
    if not autostart_supported():
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                        winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def make_icon(color: str, size: int = 64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, size - 4, size - 4), fill=color, outline="white", width=4)
    return img


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(self.format(record))


# --------------------------------------------------------------------------- the window


class App:
    def __init__(self, root: tk.Tk, minimized: bool):
        self.root = root
        self.cfg_path = data_dir() / "onair.toml"
        self.app = self._load()
        self.bridge: ob.Bridge | None = None
        self.thread: threading.Thread | None = None
        self.stop_evt: threading.Event | None = None
        self.logq: queue.Queue = queue.Queue()
        self._learning, self._resume, self._n = 0, False, 0
        self.ui_q: queue.Queue = queue.Queue()  # tray-thread -> Tk thread
        self.tray = None
        self._tray_state = None
        self._closing = False
        self._told_about_tray = False
        self._setup_logging()

        root.title("On-Air Sign Bridge")
        root.geometry("840x820")
        root.minsize(760, 720)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Unmap>", self._on_unmap)
        self._build()
        self._start_tray()
        self.refresh_midi_list()
        self.load_into_widgets()
        self.tick()
        if self.cfg_path.exists() and self.app.mics:
            self.start_bridge()
            if minimized:
                root.after(100, self.hide_to_tray)
        else:
            self.nb.select(self.setup_tab)

    # -- config
    def _load(self) -> ob.AppConfig:
        if self.cfg_path.exists():
            try:
                return ob.app_from_cfg(ob.load_config(self.cfg_path))
            except Exception as e:
                messagebox.showwarning("Settings", f"Could not read {self.cfg_path}:\n{e}\n\nStarting with empty settings.")
        return ob.AppConfig()

    def _setup_logging(self) -> None:
        fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
        h = QueueLogHandler(self.logq)
        h.setFormatter(fmt)
        fh = logging.handlers.RotatingFileHandler(data_dir() / "onair.log", maxBytes=500_000, backupCount=2,
                                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        ob.log.setLevel(logging.INFO)
        ob.log.addHandler(h)
        ob.log.addHandler(fh)

    # -- layout
    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        self.banner = tk.Label(top, text="Stopped", font=("Segoe UI", 20, "bold"), fg="white", bg=GREY, pady=10)
        self.banner.pack(fill="x")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(8, 0))
        self.midi_dot = tk.Label(row, text="● Desk MIDI", fg=GREY)
        self.midi_dot.pack(side="left")
        self.relay_dot = tk.Label(row, text="● Relay", fg=GREY)
        self.relay_dot.pack(side="left", padx=16)
        ttk.Button(row, text="Quit", command=self.quit).pack(side="right", padx=(6, 0))
        self.start_btn = ttk.Button(row, text="Start", command=self.start_bridge)
        self.start_btn.pack(side="right")
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop_bridge)
        self.stop_btn.pack(side="right", padx=6)
        self.live_lbl = ttk.Label(top, text="")
        self.live_lbl.pack(anchor="w", pady=(6, 0))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        self.status_tab = ttk.Frame(self.nb, padding=8)
        self.setup_tab = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.status_tab, text="  Log  ")
        self.nb.add(self.setup_tab, text="  Setup  ")

        self.log_text = tk.Text(self.status_tab, height=10, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)

        t = self.setup_tab
        # 1 desk
        f = ttk.LabelFrame(t, text="1. Desk (MIDI)", padding=8)
        f.pack(fill="x")
        self.midi_var = tk.StringVar()
        self.midi_combo = ttk.Combobox(f, textvariable=self.midi_var, state="readonly", width=48)
        self.midi_combo.pack(side="left")
        ttk.Button(f, text="Refresh", command=self.refresh_midi_list).pack(side="left", padx=6)
        ttk.Label(f, text="Pro II: 'MIDI Function'. Turn MIDI on at the desk first.",
                  foreground=GREY).pack(side="left", padx=6)

        # 2 relay
        f = ttk.LabelFrame(t, text="2. Relay", padding=8)
        f.pack(fill="x", pady=6)
        self.relay_info = ttk.Label(f, text="")
        self.relay_info.pack(side="left")
        ttk.Button(f, text="Test relays", command=self.test_relays).pack(side="right")
        ttk.Button(f, text="Driver help", command=lambda: self._open(DRIVER_URL)).pack(side="right", padx=6)

        # 3 mics
        f = ttk.LabelFrame(t, text="3. Microphones (any live mic lights every sign)", padding=8)
        f.pack(fill="both", expand=True)
        cols = ("name", "fader", "mute")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=5, selectmode="browse")
        for c, w, txt in (("name", 170, "Mic"), ("fader", 200, "Fader"), ("mute", 260, "Mute button")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w)
        self.tree.pack(fill="both", expand=True)
        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 0))
        for n, (text, cmd) in enumerate((("Add mic (learn)", self.add_mic), ("Re-learn fader", self.relearn_fader),
                                         ("Learn mute", self.learn_mute), ("Clear mute", self.clear_mute),
                                         ("Rename", self.rename_mic), ("Remove mic", self.remove_mic),
                                         ("Re-learn ALL (new desk)", self.relearn_all))):
            ttk.Button(btns, text=text, command=cmd).grid(row=n // 4, column=n % 4, padx=3, pady=2, sticky="ew")
        for c in range(4):
            btns.columnconfigure(c, weight=1)

        # 4 signs + options
        f = ttk.LabelFrame(t, text="4. Signs and options", padding=8)
        f.pack(fill="x", pady=6)
        ttk.Label(f, text="Relays with a sign connected:").grid(row=0, column=0, sticky="w")
        self.relay_vars = {}
        for i in (1, 2, 3, 4):
            v = tk.BooleanVar()
            self.relay_vars[i] = v
            ttk.Checkbutton(f, text=str(i), variable=v).grid(row=0, column=i, padx=4)
        ttk.Label(f, text="At start-up:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.unknown_var = tk.StringVar()
        ttk.Radiobutton(f, text="signs off until a mic is live", variable=self.unknown_var, value="idle").grid(
            row=1, column=1, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Radiobutton(f, text="signs on until the desk has reported", variable=self.unknown_var, value="live").grid(
            row=2, column=1, columnspan=4, sticky="w")
        ttk.Label(f, text="Keep lit after the mic goes quiet (seconds):").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.hang_var = tk.DoubleVar(value=1.5)
        ttk.Spinbox(f, from_=0, to=10, increment=0.5, width=5, textvariable=self.hang_var).grid(
            row=3, column=1, sticky="w", pady=(6, 0))
        self.auto_var = tk.BooleanVar()
        cb = ttk.Checkbutton(f, text="Start automatically when Windows starts (runs in the tray)",
                             variable=self.auto_var, command=self.apply_autostart)
        cb.grid(row=4, column=0, columnspan=5, sticky="w", pady=(6, 0))
        if not autostart_supported():
            cb.state(["disabled"])

        ttk.Button(t, text="Save settings", command=self.save).pack(anchor="e")

    # -- widgets <-> config
    def load_into_widgets(self) -> None:
        a = self.app
        names = self.midi_combo["values"]
        match = next((n for n in names if a.midi_match and a.midi_match.lower() in n.lower()), "")
        self.midi_var.set(match or a.midi_match or ob.default_midi_match(list(names)))
        for i, v in self.relay_vars.items():
            v.set(i in a.relays)
        self.unknown_var.set(a.unknown_as)
        self.hang_var.set(a.off_delay)
        self.auto_var.set(get_autostart())
        self.refresh_tree()

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, m in enumerate(self.app.mics):
            mute = "-" if not m.mute else f"{ob.describe_binding(m.mute[0])}  ({m.mute[1]})"
            self.tree.insert("", "end", iid=str(i), values=(m.name, ob.describe_binding(m.fader), mute))

    def refresh_midi_list(self) -> None:
        try:
            names = ob.get_mido().get_input_names()
        except BaseException as e:  # SystemExit from get_mido
            names = []
            self.log(f"MIDI list failed: {e}")
        self.midi_combo["values"] = names
        if names and not self.midi_var.get():
            self.midi_var.set(ob.default_midi_match(names))

    def selected(self) -> int | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Microphones", "Click a mic in the list first.")
            return None
        return int(sel[0])

    def save(self) -> bool:
        a = self.app
        a.midi_match = ob.stable_match(self.midi_var.get())
        a.relays = [i for i, v in self.relay_vars.items() if v.get()]
        a.unknown_as = self.unknown_var.get() or "idle"
        try:
            a.off_delay = max(0.0, float(self.hang_var.get()))
        except (tk.TclError, ValueError):
            a.off_delay = 1.5
        if not a.mics:
            messagebox.showinfo("Save", "Add at least one microphone first.")
            return False
        if not a.relays:
            messagebox.showinfo("Save", "Tick at least one relay that has a sign connected.")
            return False
        if not a.midi_match:
            messagebox.showinfo("Save", "Pick the desk's MIDI input first.")
            return False
        self.cfg_path.write_text(ob.config_to_toml(a), encoding="utf-8")
        try:
            set_autostart(self.auto_var.get())
        except OSError as e:
            messagebox.showwarning("Start with Windows", f"Could not change the Windows start-up setting:\n{e}")
        self.log(f"Settings saved to {self.cfg_path}")
        self.stop_bridge()
        self.start_bridge()
        self.nb.select(self.status_tab)
        return True

    # -- running
    def start_bridge(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if not self.cfg_path.exists():
            messagebox.showinfo("Start", "Set up and save the settings first (Setup tab).")
            self.nb.select(self.setup_tab)
            return
        try:
            self.bridge = ob.Bridge(ob.load_config(self.cfg_path))
        except Exception as e:
            messagebox.showerror("Start", f"The settings file is not valid:\n{e}")
            return
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(self.bridge, self.stop_evt), daemon=True)
        self.thread.start()

    def _run(self, bridge, stop) -> None:
        try:
            bridge.run(stop)
        except Exception as e:  # keep the window alive and say why
            ob.log.error("bridge crashed: %s", e)

    def stop_bridge(self) -> None:
        if self.thread and self.stop_evt:
            self.stop_evt.set()
            self.thread.join(timeout=5)
        self.thread = self.stop_evt = self.bridge = None

    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # -- learning (needs the MIDI port to ourselves)
    def learn(self, prompt: str, seconds: float, done) -> None:
        port = self.midi_var.get()
        if not port:
            messagebox.showinfo("MIDI", "Pick the desk's MIDI input first (step 1).")
            return
        if self._learning == 0:
            self._resume = self.running()  # put the bridge back when the whole learning chain is finished
            self.stop_bridge()
        self._learning += 1
        win = tk.Toplevel(self.root)
        win.title("MIDI learn")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=prompt, padding=20, font=("Segoe UI", 12), wraplength=360, justify="center").pack()
        bar = ttk.Progressbar(win, mode="indeterminate", length=320)
        bar.pack(padx=20, pady=(0, 20))
        bar.start(15)
        result: dict = {}

        def work():
            try:
                result["events"] = ob.capture_events(port, seconds)
            except BaseException as e:
                result["error"] = str(e)

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def poll():
            if t.is_alive():
                win.after(100, poll)
                return
            win.destroy()
            try:
                if "error" in result:
                    messagebox.showerror("MIDI", f"Could not open {port}:\n{result['error']}\n\n"
                                                 "Is another program (a DAW, Rode software) using it?")
                else:
                    done(result["events"])
            finally:
                self._learning -= 1
                if self._learning == 0 and self._resume:
                    self._resume = False
                    self.start_bridge()

        win.after(100, poll)

    def learn_fader_for(self, idx: int | None, name: str, then=None) -> None:
        def done(events):
            b = ob.pick_fader(events)
            if b is None:
                if messagebox.askretrycancel("MIDI learn", "Heard no fader movement. Is MIDI control switched on at the "
                                                           "desk? Try again?"):
                    self.learn_fader_for(idx, name, then)
                return
            if idx is None:
                self.app.mics.append(ob.MicSpec(name, b))
                idx2 = len(self.app.mics) - 1
            else:
                self.app.mics[idx].fader = b
                idx2 = idx
            self.refresh_tree()
            if then:
                then(idx2)

        self.learn(f"'{name}': move its FADER from bottom to top and back, slowly.", 4.0, done)

    def learn_mute_for(self, idx: int, then=None) -> None:
        m = self.app.mics[idx]

        def fin():
            self.refresh_tree()
            if then:
                then()

        def done1(events):
            hit = next((e for e in events if e.value > 0 and ob._key(e) != ob._key_of(m.fader)), None)
            if hit is None:
                messagebox.showinfo("MIDI learn", "Heard no mute press.")
                return fin()
            b = ob.Binding(hit.kind, hit.channel, hit.number)
            if any(e.value == 0 and ob._key(e) == ob._key(hit) for e in events):
                m.mute = (b, "toggle")  # a button that sends press then release: every press flips it
                return fin()

            def done2(ev2):
                again = any(e.value > 0 for e in ev2 if ob._key(e) == ob._key(hit))
                m.mute = (b, "toggle" if again else "state")
                fin()

            self.learn(f"Now press MUTE again to unmute '{m.name}'.", 4.0, done2)

        self.learn(f"'{m.name}': leave it unmuted, then press its MUTE button once.", 4.0, done1)

    # -- mic buttons
    def add_mic(self) -> None:
        name = simpledialog.askstring("New mic", "Name for this mic (e.g. Presenter, Guest 1):", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        if any(m.name == name for m in self.app.mics):
            messagebox.showinfo("New mic", "There is already a mic with that name.")
            return

        def after(idx):
            if messagebox.askyesno("Mute button", f"Also use '{name}' mute button? "
                                                   "(then the sign goes off when it is muted)"):
                self.learn_mute_for(idx)

        self.learn_fader_for(None, name, after)

    def relearn_fader(self) -> None:
        i = self.selected()
        if i is not None:
            self.learn_fader_for(i, self.app.mics[i].name)

    def learn_mute(self) -> None:
        i = self.selected()
        if i is not None:
            self.learn_mute_for(i)

    def clear_mute(self) -> None:
        i = self.selected()
        if i is not None:
            self.app.mics[i].mute = None
            self.refresh_tree()

    def rename_mic(self) -> None:
        i = self.selected()
        if i is None:
            return
        n = simpledialog.askstring("Rename", "New name:", initialvalue=self.app.mics[i].name, parent=self.root)
        if n and n.strip() and not any(m.name == n.strip() for j, m in enumerate(self.app.mics) if j != i):
            self.app.mics[i].name = n.strip()
            self.refresh_tree()

    def remove_mic(self) -> None:
        i = self.selected()
        if i is not None and messagebox.askyesno("Remove", f"Remove '{self.app.mics[i].name}'?"):
            del self.app.mics[i]
            self.refresh_tree()

    def relearn_all(self) -> None:
        """New or replaced desk: go down the list and learn each mic's fader (and mute, if it had one) again."""
        if not self.app.mics:
            messagebox.showinfo("Re-learn", "Add the mics first.")
            return
        if not messagebox.askyesno("Re-learn ALL", "Go through every mic and learn its fader again from the "
                                                   "(new) desk. Continue?"):
            return
        order = list(range(len(self.app.mics)))

        def step(k):
            if k >= len(order):
                messagebox.showinfo("Re-learn", "Done. Press Save settings.")
                return
            i = order[k]
            m = self.app.mics[i]
            had_mute = m.mute is not None

            def after_fader(_):
                if had_mute:
                    m.mute = None
                    self.learn_mute_for(i, then=lambda: step(k + 1))
                else:
                    step(k + 1)

            self.learn_fader_for(i, m.name, after_fader)

        step(0)

    # -- relay test
    def test_relays(self) -> None:
        if not messagebox.askokcancel("Test relays", "This switches relays 1-4 on and off, one at a time. Anything "
                                                      "connected to them will switch. Continue?"):
            return
        was = self.running()
        self.stop_bridge()
        port = self.app.relay_port or "auto"
        self.log("Testing relays...")

        def work():
            r = ob.SerialRelay(port, retry=0)
            try:
                for ch in (1, 2, 3, 4):
                    for want in (True, False):
                        if not r.apply({ch: want}, force=True):
                            self.logq.put("Relay test failed: relay not reachable (see messages above).")
                            return
                        self.logq.put(f"relay {ch} {'ON' if want else 'off'}")
                        threading.Event().wait(0.8)
                self.logq.put("Relay test finished: all four relays answered.")
            finally:
                for ch in (1, 2, 3, 4):
                    try:
                        r._cmd(f"AT+CH{ch}=0")
                    except Exception:
                        pass
                r.close()

        def go():
            work()
            if was:
                self.root.after(0, self.start_bridge)

        threading.Thread(target=go, daemon=True).start()
        self.nb.select(self.status_tab)

    # -- periodic UI update
    def log(self, text: str) -> None:
        self.logq.put(text)

    def tick(self) -> None:
        while True:
            try:
                line = self.logq.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        while True:
            try:
                self.ui_q.get_nowait()()
            except queue.Empty:
                break
            except Exception as e:
                self.log(f"tray action failed: {e}")
        if self._closing:
            return
        snap = self.bridge.snapshot if self.bridge and self.running() else None
        if snap is None:
            self.update_tray("stopped", "stopped", GREY)
            self.banner.configure(text="Stopped", bg=GREY)
            self.midi_dot.configure(fg=GREY)
            self.relay_dot.configure(fg=GREY)
            self.live_lbl.configure(text="Signs are off while stopped.")
            self.start_btn.state(["!disabled"])
            self.stop_btn.state(["disabled"])
        else:
            on = any(s["on"] for s in snap["signs"])
            live = sorted({m for s in snap["signs"] for m in s["live"]})
            self.banner.configure(text="ON AIR" if on else "Signs off", bg=RED if on else GREEN)
            self.update_tray("on" if on else "off", "ON AIR" if on else "signs off", RED if on else GREEN)
            self.midi_dot.configure(fg=GREEN if snap["midi"] else AMBER,
                                    text="● Desk MIDI" if snap["midi"] else "● Desk MIDI: not found")
            self.relay_dot.configure(fg=GREEN if snap["relay"] else RED,
                                     text="● Relay" if snap["relay"] else "● Relay: not connected")
            self.live_lbl.configure(text=("Live now: " + ", ".join(live)) if live else
                                    ("Waiting to hear from the desk..." if on else "No mic is live."))
            self.start_btn.state(["disabled"])
            self.stop_btn.state(["!disabled"])
        self._n += 1
        if self._n % 12 == 1:
            try:
                self.relay_info.configure(text=f"Found on {ob.find_serial_port()}")
            except Exception:
                self.relay_info.configure(text="Not found. Plug it in (driver help ->)")
        self.root.after(250, self.tick)

    # -- tray and background running
    def ui(self, fn) -> None:
        """Called from the tray thread: run fn on the Tk thread."""
        self.ui_q.put(fn)

    def _start_tray(self) -> None:
        if pystray is None:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Open On-Air Bridge", lambda *_: self.ui(self.show_window), default=True),
                pystray.MenuItem("Start with Windows", lambda *_: self.ui(self.toggle_autostart),
                                 checked=lambda *_: get_autostart(), enabled=autostart_supported()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit (signs stop)", lambda *_: self.ui(self.quit)))
            self.tray = pystray.Icon(APP_NAME, make_icon(GREY), "On-Air Sign Bridge: stopped", menu)
            self.tray.run_detached()
        except Exception as e:
            self.tray = None
            self.log(f"Tray icon unavailable ({e}); the window will stay in the taskbar.")

    def hide_to_tray(self) -> None:
        if self.tray is None:
            self.root.iconify()
            return
        self.root.withdraw()
        if not self._told_about_tray:
            self._told_about_tray = True
            try:
                self.tray.notify("Still running in the background. Click the tray icon to open it.",
                                 "On-Air Sign Bridge")
            except Exception:
                pass

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _on_unmap(self, event) -> None:
        if event.widget is self.root and self.tray is not None and self.root.state() == "iconic":
            self.root.after(50, self.hide_to_tray)  # minimise goes to the tray, not the taskbar

    def apply_autostart(self) -> None:
        try:
            set_autostart(self.auto_var.get())
            self.log("Will start with Windows." if self.auto_var.get() else "Will not start with Windows.")
        except OSError as e:
            self.auto_var.set(get_autostart())
            messagebox.showwarning("Start with Windows", f"Could not change the Windows start-up setting:\n{e}")

    def toggle_autostart(self) -> None:
        self.auto_var.set(not get_autostart())
        self.apply_autostart()

    def update_tray(self, key: str, text: str, color: str) -> None:
        if self.tray is not None and key != self._tray_state:
            self._tray_state = key
            try:
                self.tray.icon = make_icon(color)
                self.tray.title = f"On-Air Sign Bridge: {text}"
            except Exception:
                pass

    def quit(self) -> None:
        if self.running():
            self.show_window()
            if not messagebox.askyesno("Quit", "Quitting stops the on-air signs working.\n\nStop and quit?"):
                return
        self._closing = True
        self.stop_bridge()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # -- misc
    def _open(self, url: str) -> None:
        import webbrowser

        webbrowser.open(url)

    def on_close(self) -> None:
        """The X button keeps running in the tray; Quit (button or tray menu) is the way to stop."""
        if self.tray is not None:
            self.hide_to_tray()
        else:
            self.quit()


def acquire_single_instance():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return None
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minimized", action="store_true")
    args = ap.parse_args()
    lock = acquire_single_instance()
    root = tk.Tk()
    if lock is None:
        root.withdraw()
        messagebox.showinfo("On-Air Sign Bridge", "It is already running. Look for it on the taskbar.")
        return
    App(root, args.minimized)
    root.mainloop()


if __name__ == "__main__":
    main()
