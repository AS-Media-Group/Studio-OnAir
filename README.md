# On-air signs from mic faders (SH-UR04A bridge)

One small Python script on the studio computer: listens to the desk's MIDI, decides which mics are live, and switches the SH-UR04A relays over its USB serial port. Tested here against simulated MIDI and a fake relay only (`python -m unittest`, plus a dry run). **Not yet run against the real desk or relay.**

## The easy way: the Windows app

`OnAirBridge.exe` is one file with a window: pick the desk, press a button per mic to learn its fader, tick the relays, Save. No Python, no config files.

**Making the .exe.** It has to be built on Windows. Either:
- on GitHub: Actions > "Build Windows app" > Run workflow, then download `OnAirBridge-windows` from the run page (pushing a tag like `v0.1.0` also attaches it to a Release), or
- on any Windows PC with Python 3.12, double-click `build.bat`; the file appears in `dist\OnAirBridge.exe`.

**Setting up the studio PC.**
1. Copy `OnAirBridge.exe` over and double-click it. (Windows may say "protected your PC": More info, Run anyway. The file is not code-signed.) Plug the relay in; if "Relay" stays red, press Driver help.
2. Turn MIDI on at the RODECaster (Pro: Settings > Advanced > Audio > Operations > MIDI Control; Pro II: it appears as "MIDI Function").
3. **Setup** tab. Step 1: pick the desk's MIDI input. Step 3: **Add mic (learn)**, type a name, move that mic's fader when asked; say yes to the mute button question if you want mute to turn the sign off. Repeat per mic. Step 4: tick the relays that have a sign; choose start-up behaviour and tick "Start automatically when Windows starts".
4. **Test relays** (signs will flash), then **Save settings**. The big banner goes red ON AIR when any mic is live.

**Desk changed or replaced?** Setup > **Re-learn ALL (new desk)** walks down the list and learns every fader and mute again; or select one mic and use Re-learn fader / Learn mute. Then Save.

**Runs in the background.** Minimise or the X button hides the window to a **tray icon** (bottom right, maybe under the ^ arrow) and the signs keep working. The icon is red when ON AIR, green when the signs are off, grey when stopped. Click it to reopen; right-click for "Start with Windows" and "Quit". Only Quit (tray menu or the Quit button) stops the signs. Ticking "Start automatically when Windows starts" (Setup step 4, or the tray menu) makes it launch at login straight into the tray and start working. It starts when the user logs in, not before, so set the studio PC to log in automatically.

**Good to know.** Only one copy can run. Settings are in `%APPDATA%\OnAirBridge\onair.toml` and a log is beside it in `onair.log`. Windows lets only one program hold the desk's MIDI port, so the window pauses the bridge while you learn or test, then resumes. The window was tested here on Linux with simulated MIDI, a fake relay and a fake tray; the Windows build, the real tray icon, the minimise button, the Windows start-up entry and the real desk and relay are untested until you run them.

# Command-line version (advanced)

## How the relay is driven (SH-UR04A datasheet v1.0, DSD TECH)

- Chip CP2102, shows up as a serial port. 9600 baud, 8 data bits, no parity, 1 stop bit.
- ASCII commands: `AT` (replies `OK`), `AT+CH1=1` on, `AT+CH1=0` off, `AT+CH1=?` query. Channels 1 to 4.
- Line ending `\r\n`, and give the port about 0.25 s after opening before the first write. Both come from a third-party write-up of the same board, not the datasheet; `test-relay` proves them on your unit.
- `AT+BAUD=115200` exists in the datasheet. The script never sends it.
- Relay contacts per channel: COM, NO (open when off), NC (closed when off). 10 A resistive max, AC 110-250 V or DC 30 V.
- The datasheet says nothing about the state the relays take at power-up, so the script sets every relay explicitly on start.

## Install (the Windows studio PC)

1. Plug in the relay. If a new "COM" port does not appear in Device Manager (Ports), install the CP210x driver: <https://www.deshide.com/News-detail_CP210x_VCP_Drivers.html> (or Silicon Labs' own).
2. Install **Python 3.12** from python.org (3.11 also works; **not 3.13 or newer**, because `python-rtmidi` has no Windows download for those and pip then tries to build it and fails with "error generating package metadata"). If you already have a newer Python, install 3.12 alongside it. Then in Command Prompt, in the folder from step 3:
   ```
   py -3.12 -m venv venv
   venv\Scripts\activate
   pip install pyserial mido python-rtmidi
   ```
   Every command below is then run in that same window after `venv\Scripts\activate`.
3. Put these files in a folder, e.g. `C:\onair`.
4. On the RODECaster, turn MIDI on. **Pro:** Settings > Advanced > Audio > Operations > MIDI Control. **Pro II / Duo:** it shows up as the MIDI device "MIDI Function".
5. Close anything else that has the RODECaster's MIDI port open (a DAW, Rode software, OBS plugins). Windows lets only one program hold a MIDI input at a time.

## Set it up (about ten minutes at the desk)

```
cd C:\onair
python onair_bridge.py list                              # note the desk's MIDI name
python onair_bridge.py setup --port "RODECaster"         # asks you to move each mic fader / press mute, writes onair.toml
python onair_bridge.py run -c onair.toml --dry-run       # logs ON AIR / off, touches no relay. Walk through every mic.
python onair_bridge.py test-relay                        # clicks each relay once
python onair_bridge.py run -c onair.toml
```

Use whatever `list` shows for the name in `--port` (any part of it, e.g. `MIDI Function` on a Pro II). `setup` is the guided path. `learn` is there if you want to see raw messages. `onair.example.toml` documents every option.

## What the RODECaster sends

- Rode's own page for the **Pro II / Duo** lists the mute button as CC 27, channels 1-6, value 1 = ON and 0 = OFF, and also sends the channel and listen buttons (CC 20, CC 24). It does not list fader messages.
- One RadioDJ forum user reports that on a Pro II every control sends MIDI, including the faders as CC 0-127 on channels 1-6. That is one report, so `setup` finds the real numbers rather than assuming them.
- For the **original Pro**, Rode says MIDI control (firmware 2.1.2 or later) syncs the desk's faders with a DAW. I found no published message list, so again `setup` finds it.
- Which of the two is a fader-only mic (no mute button) is up to you: a mic with just a fader is live when the fader is up; add its mute button in `setup` if you also want mute to turn the sign off.

## Wiring the 5 V signs

The relay is a dry switch with no power of its own to give. Run the sign's **+5 V** wire from the 5 V supply into **COM**, and from **NO** on to the sign; the ground goes straight from the supply to the sign. Use a proper 5 V supply for the signs, not the relay board's USB-C. One relay can switch several signs in parallel if the supply can carry them all (the relay is rated 10 A resistive, DC up to 30 V). `setup` asks which relays have signs and puts every mic on all of them, so any live mic lights every sign.

## Decisions the config makes for you

| Question | Answer in `onair.toml` |
|---|---|
| When is a fader "open"? | `open_above` / `close_below`, as a fraction of travel. Default opens at 8%, closes below 4%. The gap stops flicker when a fader sits near the threshold. |
| Fader or mute button? | Give a mic a `fader`, a `mute`, or both. Live means fader open **and** not muted. Mute mode `state` if the desk sends a value, `toggle` if every press flips it (toggle can drift out of step); `setup` works out which. |
| Several mics, one sign | A sign lists its mics and is on while **any** is live. `off_delay` (default 1.5 s) keeps it lit briefly after the last mic goes quiet. |
| One mic, several signs | List that mic in each sign. |
| Start-up, or desk unplugged | MIDI only reports a fader when it moves, so at start the bridge does not know where the faders are. `unknown_as = "idle"` (the default) keeps signs dark until a mic is heard to be live; `"live"` keeps them lit until it has heard each mic. With `"idle"`, a fader already up when the bridge starts will not light the sign until it is moved or mute is pressed. |
| Bridge stopped normally | `on_exit` = `off`, `on` or `leave`. |
| Relay or USB lost | The bridge retries every 2 s and re-sends the wanted state on reconnect, and re-asserts it every 30 s anyway. |

## Failsafe, in plain terms

- Wire each sign through **COM and NO**. The module is USB powered, so if the computer is off or the USB cable is pulled the relays should drop out and the signs go dark.
- The module has no watchdog. If the script is killed hard, the relays hold their last state. Run it under the autostart below so it restarts and re-sets them within seconds.
- A lit sign with nobody live is a false alarm; a dark sign with a live mic is the real fault. The default is dark at start-up; set `unknown_as = "live"` to fail towards lit instead.
- If a sign is ever swapped for a mains one, that wiring is for a qualified electrician.

## Autostart (Windows)

Task Scheduler > Create Task. Trigger: "At log on" (or "At startup" if the PC auto-logs in). Action: start `C:\onair\venv\Scripts\python.exe` with arguments `onair_bridge.py run -c onair.toml`, "Start in" `C:\onair`. On Settings, tick "If the task fails, restart every 1 minute" and untick "Stop the task if it runs longer than". Lock the PC rather than logging out so the task keeps running.
