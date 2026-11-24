"""Logic tests, no hardware needed:  python3 -m unittest -v"""
import unittest

import onair_bridge as ob
from onair_bridge import Binding, Engine, Event, Mic, Sign

FADER = Binding("cc", 1, 1)
MUTE = Binding("note", 1, 16)


def cc(n, v, ch=1):
    return Event("cc", ch, n, v / 127)


class MicTests(unittest.TestCase):
    def test_fader_hysteresis(self):
        m = Mic("a", fader=Binding("cc", 1, 1), open_above=0.10, close_below=0.05)
        self.assertIsNone(m.live())
        m.feed(cc(1, 20)); self.assertTrue(m.live())
        m.feed(cc(1, 9));  self.assertTrue(m.live())   # between thresholds: holds
        m.feed(cc(1, 4));  self.assertFalse(m.live())
        m.feed(cc(1, 9));  self.assertFalse(m.live())  # still holds closed
        m.feed(cc(1, 100)); self.assertTrue(m.live())

    def test_pitchbend_fader_normalised(self):
        m = Mic("a", fader=Binding("pitchbend", 2))
        m.feed(Event("pitchbend", 2, None, 0.0)); self.assertFalse(m.live())
        m.feed(Event("pitchbend", 2, None, 0.5)); self.assertTrue(m.live())
        m.feed(Event("pitchbend", 3, None, 0.0)); self.assertTrue(m.live())  # other channel ignored

    def test_fader_and_mute(self):
        m = Mic("a", fader=FADER, mute=MUTE)
        m.feed(cc(1, 100))
        self.assertTrue(m.live())  # mute never reported: assumed unmuted
        m.feed(Event("note", 1, 16, 1.0)); self.assertFalse(m.live())  # state mode, high = muted
        m.feed(Event("note", 1, 16, 0.0)); self.assertTrue(m.live())

    def test_mute_toggle_ignores_release(self):
        m = Mic("a", fader=FADER, mute=MUTE, mute_mode="toggle")
        m.feed(cc(1, 100))
        m.feed(Event("note", 1, 16, 1.0)); m.feed(Event("note", 1, 16, 0.0))
        self.assertFalse(m.live())   # one press = muted
        m.feed(Event("note", 1, 16, 1.0)); m.feed(Event("note", 1, 16, 0.0))
        self.assertTrue(m.live())    # second press = unmuted

    def test_mute_only_mic_unmuted_high(self):
        m = Mic("a", mute=Binding("cc", 1, 9), high_means="unmuted")
        self.assertIsNone(m.live())
        m.feed(cc(9, 127)); self.assertTrue(m.live())
        m.feed(cc(9, 0));   self.assertFalse(m.live())

    def test_rejects_bad_config(self):
        with self.assertRaises(ValueError): Mic("a")
        with self.assertRaises(ValueError): Mic("a", fader=FADER, open_above=0.05, close_below=0.05)


class RodeStyleTests(unittest.TestCase):
    def test_mute_value_one_counts_as_on(self):
        # RODECaster Pro II: Mute button is CC 27, value 1 = ON, 0 = OFF
        m = Mic("a", fader=Binding("cc", 1, 5), mute=Binding("cc", 1, 27))
        m.feed(Event("cc", 1, 5, 1.0)); self.assertTrue(m.live())
        m.feed(Event("cc", 1, 27, 1 / 127)); self.assertFalse(m.live())
        m.feed(Event("cc", 1, 27, 0.0)); self.assertTrue(m.live())


class WizardTests(unittest.TestCase):
    def run_wizard(self, captures, answers):
        caps, ans, said = iter(captures), iter(answers), []
        text = ob.run_setup(lambda s: next(caps), lambda prompt: next(ans), said.append, "RODECaster")
        import tomllib
        return text, tomllib.loads(text), said

    def sweep(self, ch, num):
        return [Event("cc", ch, num, v / 127) for v in (0, 20, 60, 127, 60, 0)]

    def test_two_mics_one_with_state_mute_all_relays(self):
        captures = [
            self.sweep(1, 5),                                  # Host fader
            self.sweep(2, 5),                                  # Guest fader
            [Event("cc", 2, 27, 1 / 127)],                     # Guest mute press 1: value stays on
            [Event("cc", 2, 27, 0.0)],                         # press 2: off -> state mode
        ]
        answers = ["Host, Guest", "", "n", "", "y", "", "", "1,2,3,4"]
        # prompts in order: names, Host fader, Host mute?, Guest fader, Guest mute?, press1, press2, relays
        text, cfg, _ = self.run_wizard(captures, answers)
        self.assertEqual(cfg["mic"][0]["fader"], {"type": "cc", "channel": 1, "number": 5})
        self.assertNotIn("mute", cfg["mic"][0])
        self.assertEqual(cfg["mic"][1]["mute"]["number"], 27)
        self.assertEqual(cfg["mic"][1]["mute"]["mode"], "state")
        self.assertEqual([s["relay"] for s in cfg["sign"]], [1, 2, 3, 4])
        self.assertEqual(cfg["sign"][0]["mics"], ["Host", "Guest"])
        eng = ob.build_engine(cfg)                             # the generated file is valid
        self.assertEqual(len(eng.signs), 4)

    def test_momentary_mute_detected_as_toggle(self):
        captures = [self.sweep(1, 5), [Event("note", 1, 16, 1.0), Event("note", 1, 16, 0.0)]]
        _, cfg, _ = self.run_wizard(captures, ["Host", "", "y", "", ""])
        self.assertEqual(cfg["mic"][0]["mute"]["mode"], "toggle")

    def test_fader_not_heard_retries(self):
        captures = [[], self.sweep(1, 5)]
        _, cfg, said = self.run_wizard(captures, ["Host", "", "", "n", ""])
        self.assertEqual(cfg["mic"][0]["fader"]["number"], 5)
        self.assertTrue(any("Heard nothing" in x for x in said))


class AppConfigTests(unittest.TestCase):
    def test_roundtrip_through_toml(self):
        import tomllib
        app = ob.AppConfig('RODE "Caster"', [
            ob.MicSpec("Host", Binding("cc", 1, 5)),
            ob.MicSpec("Guest", Binding("pitchbend", 2), (Binding("cc", 2, 27), "state"))],
            [1, 3], unknown_as="live", off_delay=2.5)
        cfg = tomllib.loads(ob.config_to_toml(app))
        ob.build_engine(cfg)  # valid for the bridge
        back = ob.app_from_cfg(cfg)
        self.assertEqual(back.midi_match, 'RODE "Caster"')
        self.assertEqual(back.relays, [1, 3])
        self.assertEqual((back.unknown_as, back.off_delay), ("live", 2.5))
        self.assertEqual(back.mics[1].mute, (Binding("cc", 2, 27), "state"))
        self.assertEqual(back.mics[1].fader, Binding("pitchbend", 2, None))

    def test_detect_mute_state_and_none(self):
        calls = []
        r = ob.detect_mute(lambda: [Event("cc", 1, 27, 1 / 127)] if not calls else [Event("cc", 1, 27, 0.0)],
                           lambda: calls.append(1))
        self.assertEqual(r, (Binding("cc", 1, 27), "state"))
        self.assertIsNone(ob.detect_mute(lambda: [], lambda: None))

    def test_midi_name_helpers(self):
        self.assertEqual(ob.stable_match("MIDI Function 1"), "MIDI Function")
        self.assertEqual(ob.default_midi_match(["Microsoft GS", "RODECaster Pro II MIDI 2"]), "RODECaster Pro II MIDI 2")
        self.assertEqual(ob.default_midi_match([]), "")


class BridgeTests(unittest.TestCase):
    def test_bridge_runs_in_a_thread_and_reports(self):
        import threading, time as _t
        cfg = {"midi": {"port": "x"}, "relay": {}, "safety": {"on_exit": "off"},
               "mic": [{"name": "A", "fader": {"type": "cc", "channel": 1, "number": 1}}],
               "sign": [{"name": "S", "relay": 1, "mics": ["A"], "off_delay": 0}]}
        b = ob.Bridge(cfg, dry_run=True)
        b.midi = types_ns(connected=True, lost=False, poll=lambda: None, close=lambda: None)
        stop = threading.Event()
        t = threading.Thread(target=b.run, args=(stop,)); t.start()
        b.q.put(__import__("types").SimpleNamespace(type="control_change", channel=0, control=1, value=100))
        _t.sleep(0.4)
        self.assertTrue(b.snapshot["signs"][0]["on"]); self.assertEqual(b.snapshot["signs"][0]["live"], ["A"])
        stop.set(); t.join(2)
        self.assertFalse(t.is_alive()); self.assertEqual(b.relay.sent[1], False)  # off on exit


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class SignTests(unittest.TestCase):
    def make(self, **kw):
        a, b = Mic("a", fader=Binding("cc", 1, 1)), Mic("b", fader=Binding("cc", 1, 2))
        return a, b, Sign("door", 1, [a, b], **kw)

    def test_any_mic_lights_sign_and_hang_time(self):
        a, b, s = self.make(off_delay=2.0)
        a.feed(cc(1, 0)); b.feed(cc(2, 0)); s.prime()
        a.feed(cc(1, 100)); self.assertTrue(s.update(0.0))
        b.feed(cc(2, 100)); self.assertTrue(s.update(1.0))
        a.feed(cc(1, 0));   self.assertTrue(s.update(2.0))    # b still up
        b.feed(cc(2, 0))
        self.assertTrue(s.update(3.0))                         # hang time starts
        self.assertTrue(s.update(4.9))
        self.assertFalse(s.update(5.0))

    def test_hang_time_cancelled_by_reopen(self):
        a, b, s = self.make(off_delay=2.0)
        a.feed(cc(1, 100)); b.feed(cc(2, 0)); s.update(0.0)
        a.feed(cc(1, 0)); s.update(1.0)
        a.feed(cc(1, 100)); s.update(2.0)
        a.feed(cc(1, 0)); self.assertTrue(s.update(3.0))       # timer restarted at 3.0
        self.assertFalse(s.update(5.0))

    def test_on_delay(self):
        a, b, s = self.make(on_delay=0.5)
        a.feed(cc(1, 0)); b.feed(cc(2, 0)); s.prime()
        a.feed(cc(1, 100))
        self.assertFalse(s.update(0.0)); self.assertFalse(s.update(0.4)); self.assertTrue(s.update(0.5))

    def test_unknown_policy(self):
        for policy, expect in (("idle", False), ("live", True)):
            a, b, s = self.make(unknown_as=policy)
            s.prime()
            self.assertEqual(s.state, expect)
        a, b, s = self.make(unknown_as="live", off_delay=0.0)
        a.feed(cc(1, 0)); s.prime()
        self.assertTrue(s.state)                  # b still unknown: stays lit
        b.feed(cc(2, 0))
        self.assertFalse(s.update(10.0))          # both heard and quiet: goes dark

    def test_engine_forget_then_lit_when_fail_lit(self):
        a, b, s = self.make(unknown_as="live", off_delay=0.0)
        e = Engine([a, b], [s])
        e.feed(cc(1, 0)); e.feed(cc(2, 0)); e.prime()
        self.assertFalse(s.state)
        e.forget()
        self.assertTrue(e.update(1.0)[1])


class ConfigTests(unittest.TestCase):
    BASE = {
        "safety": {}, "midi": {}, "relay": {},
        "mic": [{"name": "A", "fader": {"type": "cc", "channel": 1, "number": 1}},
                {"name": "B", "fader": {"type": "cc", "channel": 1, "number": 2}}],
        "sign": [{"name": "Door", "relay": 1, "mics": ["A", "B"]}],
    }

    def test_builds(self):
        e = ob.build_engine(self.BASE)
        self.assertEqual([s.relay for s in e.signs], [1])

    def test_unknown_mic(self):
        bad = {**self.BASE, "sign": [{"name": "Door", "relay": 1, "mics": ["Nope"]}]}
        with self.assertRaises(ValueError): ob.build_engine(bad)

    def test_duplicate_relay(self):
        bad = {**self.BASE, "sign": [{"name": "x", "relay": 1, "mics": ["A"]}, {"name": "y", "relay": 1, "mics": ["B"]}]}
        with self.assertRaises(ValueError): ob.build_engine(bad)

    def test_example_file_loads(self):
        from pathlib import Path
        ob.build_engine(ob.load_config(Path(__file__).with_name("onair.example.toml")))


class FakeSerial:
    def __init__(self, fail_after=None):
        self.written, self.fail_after, self.closed = [], fail_after, False
        self._reply = b""

    def write(self, data):
        if self.fail_after is not None and len(self.written) >= self.fail_after:
            raise OSError("unplugged")
        self.written.append(data.decode().strip())
        cmd = self.written[-1]
        self._reply = b"OK\n\x00" if cmd == "AT" else (b"OK+" + cmd[3:].encode() + b"\n\x00")

    def read_until(self, _):
        return self._reply

    def close(self):
        self.closed = True


class RelayTests(unittest.TestCase):
    def relay(self, fake):
        r = ob.SerialRelay("fake", refresh=1000, retry=0)
        r._open = lambda: setattr(r, "ser", fake)  # skip real port open
        return r

    def test_only_sends_changes(self):
        f = FakeSerial(); r = self.relay(f)
        r.apply({1: False, 2: False}, force=True)
        n = len(f.written)
        r.apply({1: False, 2: False}); self.assertEqual(len(f.written), n)
        r.apply({1: True, 2: False});  self.assertEqual(f.written[n:], ["AT+CH1=1"])

    def test_reasserts_everything_after_reconnect(self):
        f = FakeSerial(fail_after=1); r = self.relay(f)
        self.assertTrue(r.apply({1: True}, force=True))
        self.assertFalse(r.apply({1: False}))          # write fails -> closed, state forgotten
        self.assertIsNone(r.ser)
        f2 = FakeSerial(); r._open = lambda: setattr(r, "ser", f2)
        self.assertTrue(r.apply({1: True}))
        self.assertEqual(f2.written, ["AT+CH1=1"])     # re-sent although "unchanged" from the caller's view


if __name__ == "__main__":
    unittest.main()
