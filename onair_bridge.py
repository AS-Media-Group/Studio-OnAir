#!/usr/bin/env python3
"""onair_bridge: drive DSD TECH SH-UR04A relays (on-air signs) from MIDI mic faders and mutes.

Runs on the same computer as the desk and the relay. Needs Python 3.11 or 3.12 (python-rtmidi has no Windows build for 3.13+) and
    pip install pyserial mido python-rtmidi

    python3 onair_bridge.py list                       what MIDI inputs and serial ports exist
    python3 onair_bridge.py setup --port "name"        guided: asks you to move each fader / press mute, writes onair.toml
    python3 onair_bridge.py learn --port "desk name"   print what the desk sends (find your CC / note numbers)
    python3 onair_bridge.py run -c onair.toml --dry-run   full logic, no relay touched
    python3 onair_bridge.py run -c onair.toml          the real thing
    python3 onair_bridge.py test-relay                 clicks each relay once (signs will flash)

Relay protocol (SH-UR04A datasheet v1.0, "AT Command List"): 9600 8N1 over the CP2102 virtual COM port,
    AT            -> OK
    AT+CH<n>=1/0  -> relay n on / off      (the unit answers OK+CH<n>=<state>)
    AT+CH<n>=?    -> query relay n
The datasheet also lists AT+BAUD=115200. This script never sends it.
"""
from __future__ import annotations

import argparse
import logging
import queue
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.2.0"

log = logging.getLogger("onair")

# --------------------------------------------------------------------------- MIDI backend


def get_mido():
    """mido with the python-rtmidi backend chosen explicitly (also what the packaged .exe needs)."""
    import mido

    try:
        mido.set_backend("mido.backends.rtmidi")
    except ImportError as e:
        raise SystemExit("python-rtmidi is not installed (pip install python-rtmidi)") from e
    return mido


# --------------------------------------------------------------------------- MIDI events


@dataclass(frozen=True)
class Event:
    kind: str  # "cc" | "note" | "pitchbend"
    channel: int  # 1-16, as people count them
    number: int | None  # CC or note number; None for pitchbend
    value: float  # normalised 0.0-1.0


def event_from_mido(msg) -> Event | None:
    t = msg.type
    if t == "control_change":
        return Event("cc", msg.channel + 1, msg.control, msg.value / 127)
    if t == "note_on":
        return Event("note", msg.channel + 1, msg.note, msg.velocity / 127)
    if t == "note_off":
        return Event("note", msg.channel + 1, msg.note, 0.0)
    if t == "pitchwheel":
        return Event("pitchbend", msg.channel + 1, None, (msg.pitch + 8192) / 16383)
    return None


@dataclass(frozen=True)
class Binding:
    kind: str
    channel: int | None = None  # None = any channel
    number: int | None = None

    @staticmethod
    def from_dict(d: dict, what: str) -> "Binding":
        kind = d.get("type")
        if kind not in ("cc", "note", "pitchbend"):
            raise ValueError(f"{what}: type must be cc, note or pitchbend (got {kind!r})")
        if kind != "pitchbend" and d.get("number") is None:
            raise ValueError(f"{what}: {kind} needs a number")
        ch = d.get("channel")
        if ch is not None and not 1 <= ch <= 16:
            raise ValueError(f"{what}: channel is 1-16")
        return Binding(kind, ch, d.get("number"))

    def matches(self, ev: Event) -> bool:
        return (
            ev.kind == self.kind
            and (self.channel is None or ev.channel == self.channel)
            and (self.kind == "pitchbend" or ev.number == self.number)
        )


# --------------------------------------------------------------------------- logic (no I/O)


class Mic:
    """One microphone: a fader binding, a mute binding, or both. live = fader open AND not muted."""

    def __init__(self, name, fader=None, mute=None, open_above=0.08, close_below=0.04,
                 mute_mode="state", high_means="muted"):
        if fader is None and mute is None:
            raise ValueError(f"mic {name!r}: needs a fader and/or a mute binding")
        if open_above <= close_below:
            raise ValueError(f"mic {name!r}: open_above must be greater than close_below")
        if mute_mode not in ("state", "toggle"):
            raise ValueError(f"mic {name!r}: mute mode is 'state' or 'toggle'")
        if high_means not in ("muted", "unmuted"):
            raise ValueError(f"mic {name!r}: high_means is 'muted' or 'unmuted'")
        self.name, self.fader, self.mute = name, fader, mute
        self.open_above, self.close_below = open_above, close_below
        self.mute_mode, self.high_means = mute_mode, high_means
        self.fader_open: bool | None = None
        self.muted: bool | None = None

    def feed(self, ev: Event) -> None:
        if self.fader and self.fader.matches(ev):
            if ev.value >= self.open_above:
                self.fader_open = True
            elif ev.value <= self.close_below:
                self.fader_open = False
            # between the two thresholds: keep the last state (hysteresis)
        if self.mute and self.mute.matches(ev):
            high = ev.value > 0  # any non-zero value: RODECaster sends CC 27 = 1 for mute ON
            if self.mute_mode == "toggle":
                if high:  # a press; the release (value 0) is ignored
                    self.muted = not bool(self.muted)
            else:
                self.muted = high if self.high_means == "muted" else not high

    def forget(self) -> None:
        self.fader_open = None
        self.muted = None

    def live(self) -> bool | None:
        """True / False, or None when we have not heard from the desk yet."""
        parts = []
        if self.fader:
            parts.append(self.fader_open)
        if self.mute:
            if self.muted is None:
                # Desks rarely report mute state until a button is pressed. With a fader to go on,
                # assume unmuted; a mute-only mic stays unknown.
                parts.append(True if self.fader else None)
            else:
                parts.append(not self.muted)
        if any(p is False for p in parts):
            return False
        if any(p is None for p in parts):
            return None
        return True


class Sign:
    """One on-air sign on one relay channel. On while ANY of its mics is live."""

    def __init__(self, name, relay, mics, on_delay=0.0, off_delay=1.5, unknown_as="idle"):
        if relay not in (1, 2, 3, 4):
            raise ValueError(f"sign {name!r}: relay must be 1-4")
        if unknown_as not in ("idle", "live"):
            raise ValueError(f"sign {name!r}: unknown_as is 'idle' or 'live'")
        self.name, self.relay, self.mics = name, relay, mics
        self.on_delay, self.off_delay, self.unknown_as = on_delay, off_delay, unknown_as
        self.state = False
        self._pending: tuple[bool, float] | None = None

    def wanted(self) -> bool:
        lives = [m.live() for m in self.mics]
        if any(v is True for v in lives):
            return True
        return self.unknown_as == "live" and any(v is None for v in lives)

    def live_mics(self) -> list[str]:
        return [m.name for m in self.mics if m.live() is True]

    def prime(self) -> None:
        self.state, self._pending = self.wanted(), None

    def update(self, now: float) -> bool:
        want = self.wanted()
        if want == self.state:
            self._pending = None
            return self.state
        if self._pending is None or self._pending[0] != want:
            self._pending = (want, now)
        delay = self.on_delay if want else self.off_delay
        if now - self._pending[1] >= delay:
            self.state, self._pending = want, None
        return self.state


class Engine:
    def __init__(self, mics: list[Mic], signs: list[Sign]):
        self.mics, self.signs = mics, signs

    def feed(self, ev: Event) -> None:
        for m in self.mics:
            m.feed(ev)

    def forget(self) -> None:
        for m in self.mics:
            m.forget()

    def prime(self) -> dict[int, bool]:
        for s in self.signs:
            s.prime()
        return {s.relay: s.state for s in self.signs}

    def update(self, now: float) -> dict[int, bool]:
        return {s.relay: s.update(now) for s in self.signs}


# --------------------------------------------------------------------------- config


def load_config(path: Path) -> dict:
    import tomllib

    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("midi", {})
    cfg.setdefault("relay", {})
    cfg.setdefault("safety", {})
    return cfg


def build_engine(cfg: dict) -> Engine:
    unknown_as = cfg["safety"].get("unknown_as", "idle")
    mics: dict[str, Mic] = {}
    for m in cfg.get("mic", []):
        name = m["name"]
        if name in mics:
            raise ValueError(f"mic {name!r} defined twice")
        mute = m.get("mute")
        mics[name] = Mic(
            name,
            fader=Binding.from_dict(m["fader"], f"mic {name!r} fader") if "fader" in m else None,
            mute=Binding.from_dict(mute, f"mic {name!r} mute") if mute else None,
            open_above=m.get("open_above", 0.08),
            close_below=m.get("close_below", 0.04),
            mute_mode=(mute or {}).get("mode", "state"),
            high_means=(mute or {}).get("high_means", "muted"),
        )
    signs, used = [], set()
    for s in cfg.get("sign", []):
        if s["relay"] in used:
            raise ValueError(f"relay {s['relay']} used by two signs")
        used.add(s["relay"])
        missing = [n for n in s["mics"] if n not in mics]
        if missing:
            raise ValueError(f"sign {s['name']!r}: unknown mic(s) {missing}")
        signs.append(Sign(s["name"], s["relay"], [mics[n] for n in s["mics"]],
                          on_delay=s.get("on_delay", 0.0), off_delay=s.get("off_delay", 1.5),
                          unknown_as=s.get("unknown_as", unknown_as)))
    if not signs:
        raise ValueError("config has no [[sign]] entries")
    return Engine(list(mics.values()), signs)


# --------------------------------------------------------------------------- relay


class RelayError(Exception):
    pass


# Silicon Labs CP210x (what the SH-UR04A listing says), and the CH340 that one reader's unit reported.
KNOWN_SERIAL_IDS = {(0x10C4, 0xEA60), (0x1A86, 0x7523)}


def find_serial_port() -> str:
    from serial.tools import list_ports

    cands = [p.device for p in list_ports.comports() if (p.vid, p.pid) in KNOWN_SERIAL_IDS]
    if len(cands) == 1:
        return cands[0]
    everything = ", ".join(p.device for p in list_ports.comports()) or "none"
    raise RelayError(f"expected one CP210x/CH340 serial port, found {cands or 'none'} "
                     f"(all ports: {everything}). Set [relay] port in the config.")


class DryRunRelay:
    ok = True

    def __init__(self):
        self.sent: dict[int, bool] = {}

    def apply(self, states: dict[int, bool], force: bool = False) -> bool:
        for ch, want in states.items():
            if force or self.sent.get(ch) != want:
                log.info("[dry-run] relay %d -> %s", ch, "ON" if want else "off")
                self.sent[ch] = want
        return True

    def close(self) -> None:
        pass


class SerialRelay:
    def __init__(self, port: str = "auto", refresh: float = 30.0, retry: float = 2.0):
        self.port, self.refresh, self.retry = port, refresh, retry
        self.ser = None
        self.sent: dict[int, bool] = {}
        self._last_refresh = 0.0
        self._retry_at = 0.0
        self._down_since: float | None = None  # when the relay was first lost (None = fine)
        self._last_warn = 0.0

    # -- low level
    def _open(self) -> None:
        import serial

        port = find_serial_port() if self.port == "auto" else self.port
        ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1, timeout=0.3, write_timeout=1)
        time.sleep(0.25)  # first byte is sometimes lost if we write straight away
        ser.reset_input_buffer()
        self.ser = ser
        reply = self._cmd("AT")
        if not reply.startswith("OK"):
            self.close()
            raise RelayError(f"no OK from relay on {port} (got {reply!r})")
        log.info("relay connected on %s", port)

    def _cmd(self, cmd: str) -> str:
        self.ser.write((cmd + "\r\n").encode())
        raw = self.ser.read_until(b"\n")
        return raw.decode(errors="replace").replace("\x00", "").strip()

    def close(self) -> None:
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    # -- public
    @property
    def ok(self) -> bool:
        return self.ser is not None

    def apply(self, states: dict[int, bool], force: bool = False) -> bool:
        now = time.monotonic()
        try:
            if self.ser is None:
                if now < self._retry_at:
                    return False
                self._open()
                self.sent.clear()
                force = True
            stale = now - self._last_refresh >= self.refresh  # periodic re-assert in case the unit glitched
            for ch, want in states.items():
                if force or stale or self.sent.get(ch) != want:
                    reply = self._cmd(f"AT+CH{ch}={int(want)}")
                    if f"CH{ch}={int(want)}" not in reply:
                        log.warning("relay %d: unexpected reply %r", ch, reply)
                    self.sent[ch] = want
            if stale or force:
                self._last_refresh = now
            if self._down_since is not None:  # only now do we know the relay really answers again
                log.info("relay is back after %.0f s", now - self._down_since)
                self._down_since = None
            return True
        except Exception as e:  # serial.SerialException, RelayError, OSError
            # Say so once, then only a reminder a minute: a relay left unplugged must not fill the log.
            if self._down_since is None:
                self._down_since = now
                self._last_warn = now
                log.error("relay problem: %s (retrying every %.0f s)", e, self.retry)
            elif now - self._last_warn >= 60:
                self._last_warn = now
                log.warning("relay still not reachable after %.0f s: %s", now - self._down_since, e)
            self.close()
            self.sent.clear()
            self._retry_at = now + self.retry
            return False


# --------------------------------------------------------------------------- MIDI input


class MidiIn:
    def __init__(self, match: str, q: "queue.Queue"):
        self.match, self.q = match.lower(), q
        self.port = None
        self.name = ""
        self.lost = False  # set when the desk disappears; the main loop clears it
        self._next = 0.0

    @property
    def connected(self) -> bool:
        return self.port is not None

    def poll(self) -> None:
        mido = get_mido()

        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + 2.0
        names = mido.get_input_names()
        if self.port is not None and self.name not in names:
            log.error("MIDI input %r disappeared", self.name)
            self.close()
            self.lost = True
        if self.port is None:
            found = [n for n in names if self.match in n.lower()]
            if found:
                self.name = found[0]
                self.port = mido.open_input(self.name, callback=self.q.put)
                log.info("MIDI input open: %s", self.name)

    def close(self) -> None:
        try:
            if self.port:
                self.port.close()
        except Exception:
            pass
        self.port = None


# --------------------------------------------------------------------------- setup wizard


def _key(ev: Event):
    return (ev.kind, ev.channel, ev.number)


def pick_fader(events: list[Event]) -> Binding | None:
    """The control that took the most different values while the fader was moved (needs at least 3)."""
    seen: dict[tuple, set] = {}
    for ev in events:
        seen.setdefault(_key(ev), set()).add(round(ev.value, 3))
    best = max(seen.items(), key=lambda kv: len(kv[1]), default=None)
    if best is None or len(best[1]) < 3:
        return None
    kind, ch, num = best[0]
    return Binding(kind, ch, num)


def describe_binding(b: Binding) -> str:
    """Plain wording for the window: 'CC 40, channel 1'."""
    ch = f", channel {b.channel}" if b.channel else ""
    if b.kind == "pitchbend":
        return f"Pitch bend{ch}"
    return f"{'CC' if b.kind == 'cc' else 'Note'} {b.number}{ch}"


def _binding_text(b: Binding) -> str:
    num = f", number = {b.number}" if b.number is not None else ""
    return f'type = "{b.kind}", channel = {b.channel}{num}'


@dataclass
class MicSpec:
    name: str
    fader: Binding
    mute: tuple[Binding, str] | None = None  # (binding, "state" | "toggle")


@dataclass
class AppConfig:
    """What the setup window edits. to_toml() writes the file the bridge reads."""
    midi_match: str = ""
    mics: list = None
    relays: list = None
    unknown_as: str = "idle"
    off_delay: float = 1.5
    relay_port: str = "auto"

    def __post_init__(self):
        self.mics = self.mics or []
        self.relays = self.relays or [1]


def config_to_toml(app: AppConfig) -> str:
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    out = ["# Written by the On-Air Bridge setup. Safe to edit by hand.", "", "[midi]", f'port = "{esc(app.midi_match)}"', "",
           "[relay]", f'port = "{esc(app.relay_port)}"', "refresh_seconds = 30", "",
           "[safety]", f'unknown_as = "{app.unknown_as}"', 'on_exit = "off"', ""]
    for m in app.mics:
        out += ["[[mic]]", f'name = "{esc(m.name)}"', f"fader = {{ {_binding_text(m.fader)} }}",
                "open_above = 0.08", "close_below = 0.04"]
        if m.mute:
            mb, mode = m.mute
            out.append(f'mute = {{ {_binding_text(mb)}, mode = "{mode}", high_means = "muted" }}')
        out.append("")
    names = ", ".join(f'"{esc(m.name)}"' for m in app.mics)
    for r in app.relays:
        out += ["[[sign]]", f'name = "Sign on relay {r}"', f"relay = {r}", f"mics = [{names}]",
                f"off_delay = {app.off_delay}", ""]
    return "\n".join(out)


def app_from_cfg(cfg: dict) -> AppConfig:
    """Load a bridge config (as read by load_config) back into the editable form."""
    mics = []
    for m in cfg.get("mic", []):
        mute = m.get("mute")
        mics.append(MicSpec(m["name"], Binding.from_dict(m["fader"], "fader"),
                            (Binding.from_dict(mute, "mute"), mute.get("mode", "state")) if mute else None))
    signs = cfg.get("sign", [])
    return AppConfig(cfg["midi"].get("port", ""), mics, sorted(s["relay"] for s in signs) or [1],
                     cfg["safety"].get("unknown_as", "idle"),
                     signs[0].get("off_delay", 1.5) if signs else 1.5,
                     cfg["relay"].get("port", "auto"))


def render_config(midi_match: str, mics: list[dict], relays: list[int]) -> str:
    return config_to_toml(AppConfig(
        midi_match, [MicSpec(m["name"], m["fader"], m.get("mute")) for m in mics], relays))


def detect_mute(capture, prompt_again, exclude: Binding | None = None) -> tuple[Binding, str] | None:
    """After the user pressed MUTE once. capture() -> events heard; prompt_again() asks for the second press.
    Returns (binding, "state" | "toggle"), or None if no press was heard."""
    first = [e for e in capture() if exclude is None or _key(e) != _key_of(exclude)]
    hit = next((e for e in first if e.value > 0), None)
    if hit is None:
        return None
    b = Binding(hit.kind, hit.channel, hit.number)
    momentary = any(e.value == 0 and _key(e) == _key(hit) for e in first)
    if not momentary:
        prompt_again()
        momentary = any(e.value > 0 for e in capture() if _key(e) == _key(hit))  # a press sent each time = toggle
    return b, ("toggle" if momentary else "state")


def default_midi_match(names: list[str]) -> str:
    for hint in ("rodecaster", "midi function", "rode"):
        for n in names:
            if hint in n.lower():
                return n
    return names[0] if names else ""


def stable_match(port_name: str) -> str:
    """Windows numbers MIDI ports ('MIDI Function 1'); keep the part that does not change."""
    import re

    return re.sub(r"\s+\d+$", "", port_name).strip()


def capture_events(port_name: str, seconds: float) -> list[Event]:
    """Open the MIDI input briefly and return what it sent. Needs the bridge stopped (Windows allows one opener)."""
    mido = get_mido()
    q: queue.Queue = queue.Queue()
    with mido.open_input(port_name, callback=q.put):
        end, got = time.monotonic() + seconds, []
        while time.monotonic() < end:
            try:
                ev = event_from_mido(q.get(timeout=0.1))
            except queue.Empty:
                continue
            if ev:
                got.append(ev)
    return got


def run_setup(capture, ask, say, midi_match: str) -> str:
    """Interactive. capture(seconds) -> list[Event]; ask(prompt) -> str; say(text). Returns the TOML text."""
    names = [n.strip() for n in ask("Mic names, comma separated, one per fader that carries a mic (e.g. Presenter, Guest 1, Guest 2): ").split(",") if n.strip()]
    if not names:
        raise SystemExit("no mics given")
    mics = []
    for name in names:
        fader = None
        while fader is None:
            ask(f"[{name}] Press Enter, then move its FADER from bottom to top and back, slowly (4 s).")
            fader = pick_fader(capture(4.0))
            if fader is None:
                say("  Heard nothing usable. Is MIDI control on in the desk's settings? Try again.")
        say(f"  fader: {_binding_text(fader)}")
        entry = {"name": name, "fader": fader, "mute": None}
        if ask(f"[{name}] Use its MUTE button too? Leave the mic unmuted first. [y/N] ").strip().lower().startswith("y"):
            ask(f"[{name}] Press Enter, then press MUTE once so the mic is muted (4 s).")
            found = detect_mute(lambda: capture(4.0),
                                lambda: ask(f"[{name}] Press Enter, then press MUTE again to unmute (4 s)."), fader)
            if found is None:
                say("  Heard no mute press, skipping mute for this mic.")
            else:
                entry["mute"] = found
                say(f"  mute: {_binding_text(found[0])}  ({found[1]})")
        mics.append(entry)
    raw = ask("Which relays have signs connected? e.g. 1 or 1,2,3,4 [1] ").strip() or "1"
    relays = sorted({int(x) for x in raw.replace(" ", "").split(",") if x in ("1", "2", "3", "4")})
    if not relays:
        raise SystemExit("no valid relay numbers")
    return render_config(midi_match, mics, relays)


def _key_of(b: Binding):
    return (b.kind, b.channel, b.number)


def cmd_setup(args) -> None:
    mido = get_mido()

    names = mido.get_input_names()
    found = [n for n in names if args.port.lower() in n.lower()]
    if not found:
        sys.exit(f"no MIDI input matching {args.port!r}. Inputs: {names}")
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} already exists; pick another name with --out or move it away")
    q: queue.Queue = queue.Queue()
    with mido.open_input(found[0], callback=q.put):
        def capture(seconds):
            while not q.empty():
                q.get_nowait()
            end, got = time.monotonic() + seconds, []
            while time.monotonic() < end:
                try:
                    ev = event_from_mido(q.get(timeout=0.1))
                except queue.Empty:
                    continue
                if ev:
                    got.append(ev)
            return got

        text = run_setup(capture, input, print, args.port)
    out.write_text(text, encoding="utf-8")
    print(f"\nWritten {out}. Next: python onair_bridge.py run -c {out} --dry-run")


# --------------------------------------------------------------------------- commands


def cmd_list(_args) -> None:
    mido = get_mido()
    from serial.tools import list_ports

    print("MIDI inputs:")
    for n in mido.get_input_names():
        print("  ", n)
    print("Serial ports:")
    for p in list_ports.comports():
        print(f"   {p.device}  vid:pid={p.vid and hex(p.vid)}:{p.pid and hex(p.pid)}  {p.description}")


def cmd_learn(args) -> None:
    mido = get_mido()

    names = mido.get_input_names()
    found = [n for n in names if args.port.lower() in n.lower()]
    if not found:
        sys.exit(f"no MIDI input matching {args.port!r}. Inputs: {names}")
    print(f"Listening on {found[0]}. Move a fader / press a mute, Ctrl-C to stop.\n")
    last: dict = {}
    with mido.open_input(found[0]) as port:
        try:
            for msg in port:
                ev = event_from_mido(msg)
                if ev is None:
                    continue
                key = (ev.kind, ev.channel, ev.number)
                if last.get(key) == round(ev.value, 3):
                    continue  # skip exact repeats
                last[key] = round(ev.value, 3)
                num = f", number = {ev.number}" if ev.number is not None else ""
                print(f'{{ type = "{ev.kind}", channel = {ev.channel}{num} }}   value {ev.value:.3f}')
        except KeyboardInterrupt:
            print()


def cmd_test_relay(args) -> None:
    input("This clicks relays 1-4 one at a time (1 s each). Anything wired to them will switch. Enter to go, Ctrl-C to stop: ")
    r = SerialRelay(args.relay_port or "auto", retry=0)
    try:
        for ch in (1, 2, 3, 4):
            for want in (True, False):
                if not r.apply({ch: want}, force=True):
                    sys.exit("relay not reachable (see error above)")
                print(f"relay {ch} {'ON ' if want else 'off'}  reply ok")
                time.sleep(1.0)
        print("Query check:", r._cmd("AT+CH1=?"))
    finally:
        for ch in (1, 2, 3, 4):
            try:
                r._cmd(f"AT+CH{ch}=0")
            except Exception:
                pass
        r.close()


class Bridge:
    """The whole job: MIDI in, logic, relay out. Blocking run() so a CLI or a window can drive it."""

    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg, self.dry_run = cfg, dry_run
        self.engine = build_engine(cfg)
        rc = cfg["relay"]
        self.relay = DryRunRelay() if dry_run else SerialRelay(
            rc.get("port", "auto"), refresh=rc.get("refresh_seconds", 30.0))
        self.on_exit = cfg["safety"].get("on_exit", "off")
        if self.on_exit not in ("off", "on", "leave"):
            raise ValueError("[safety] on_exit must be off, on or leave")
        self.q: queue.Queue = queue.Queue()
        self.midi = MidiIn(cfg["midi"].get("port", ""), self.q)
        self.snapshot: dict = {"midi": False, "relay": False, "signs": []}
        self._last_problem: tuple[str, float] = ("", 0.0)

    def _snap(self) -> None:
        self.snapshot = {
            "midi": self.midi.connected, "relay": self.relay.ok,
            "signs": [{"name": s.name, "relay": s.relay, "on": s.state, "live": s.live_mics()}
                      for s in self.engine.signs]}

    def _problem(self, what: str, err: Exception) -> None:
        """Log a repeating problem once, then at most once a minute."""
        now = time.monotonic()
        key = f"{what}:{err}"
        if key != self._last_problem[0] or now - self._last_problem[1] >= 60:
            self._last_problem = (key, now)
            log.error("%s: %s", what, err)

    def run(self, stop: threading.Event) -> None:
        engine, relay, midi = self.engine, self.relay, self.midi
        relay.apply(engine.prime(), force=True)  # known state from the first second
        shown: dict[int, bool] = {}
        log.info("running. unknown_as=%s on_exit=%s%s", self.cfg["safety"].get("unknown_as", "idle"),
                 self.on_exit, " (DRY RUN)" if self.dry_run else "")
        last_wall = time.time()
        try:
            while not stop.is_set():
                try:
                    wall = time.time()
                    if wall - last_wall > 15:
                        # The loop normally turns every 50 ms. A gap this big means the PC slept (or froze): USB and
                        # MIDI handles are not to be trusted afterwards, so start them afresh and wait to hear the desk.
                        log.warning("PC paused for %.0f s (sleep?). Reconnecting the desk and relay.", wall - last_wall)
                        midi.close()
                        engine.forget()
                        if isinstance(relay, SerialRelay):
                            relay.close()
                    last_wall = wall
                    try:
                        midi.poll()
                    except Exception as e:
                        self._problem("MIDI problem", e)
                    if midi.lost:
                        midi.lost = False
                        engine.forget()  # fader positions are unknown until the desk speaks again
                    try:
                        msg = self.q.get(timeout=0.05)
                        while True:
                            ev = event_from_mido(msg)
                            if ev:
                                engine.feed(ev)
                            msg = self.q.get_nowait()
                    except queue.Empty:
                        pass
                    relay.apply(engine.update(time.monotonic()))
                    for s in engine.signs:
                        if shown.get(s.relay) != s.state:
                            shown[s.relay] = s.state
                            why = ", ".join(s.live_mics()) or ("waiting to hear from the desk" if s.state else "-")
                            log.info("%s: %s  (live: %s)", s.name, "ON AIR" if s.state else "off", why)
                    self._snap()
                except Exception as e:  # never let one odd moment stop the signs working
                    self._problem("unexpected error (carrying on)", e)
                    stop.wait(0.5)
        finally:
            if self.on_exit != "leave":
                relay.apply({s.relay: self.on_exit == "on" for s in engine.signs}, force=True)
            midi.close()
            relay.close()
            self.snapshot = {"midi": False, "relay": False, "signs": []}
            log.info("stopped (signs set: %s)", self.on_exit)


def cmd_run(args) -> None:
    bridge = Bridge(load_config(Path(args.config)), dry_run=args.dry_run)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    bridge.run(stop)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    p = sub.add_parser("setup")
    p.add_argument("--port", required=True, help="part of the MIDI input name, e.g. RODECaster or MIDI Function")
    p.add_argument("--out", default="onair.toml")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("learn")
    p.add_argument("--port", required=True, help="part of the MIDI input name")
    p.set_defaults(fn=cmd_learn)
    p = sub.add_parser("test-relay")
    p.add_argument("--relay-port", help="serial port; default auto-detect")
    p.set_defaults(fn=cmd_test_relay)
    p = sub.add_parser("run")
    p.add_argument("-c", "--config", default="onair.toml")
    p.add_argument("--dry-run", action="store_true", help="log what would happen; never touch the relay")
    p.set_defaults(fn=cmd_run)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
