```
   ___ _   _________      __ ________   ___  _______ 
  / _ \ | / / __/ _ \____/ // / __/ /  / _ \/ __/ _ \
 / ___/ |/ / _// // /___/ _  / _// /__/ ___/ _// , _/
/_/   |___/_/ /____/   /_//_/___/____/_/  /___/_/|_| 
```

Linux audio helper for the [PioneerVFD](https://github.com/adainstarks/PioneerVFD)
Spicetify theme. Taps Spotify's PipeWire output, runs an FFT, and streams the
result to PVFD over a localhost WebSocket so the **PULSE** visualizer works
on Linux setups where Chromium's `getDisplayMedia()` picker doesn't list
Spotify or doesn't offer the "Share system audio" checkbox
(see PVFD [issue #16](https://github.com/adainstarks/PioneerVFD/issues/16)).

## Quickstart

Install:

```sh
pipx install git+https://github.com/adainstarks/PVFD-Linux-Helper.git
# or
pip install --user git+https://github.com/adainstarks/PVFD-Linux-Helper.git
```

One-shot Spotify hook (recommended — no terminal needed afterwards):

```sh
pvfd-hlpr --with-spotify
```

This writes a user-level XDG override at
`~/.local/share/applications/spotify.desktop` (or
`spotify-launcher.desktop` if you're on Arch's `extra` package) that
wraps Spotify's `Exec=` line so launching Spotify from anywhere — the KDE
menu, your dock, `spotify` in a terminal — also fires `pvfd-hlpr` in the
background. The helper exits cleanly when Spotify exits. Undo with
`pvfd-hlpr --unlink-spotify`. No `sudo`: user-level XDG entries take
priority over `/usr/share/applications/` by spec.

Then click **PULSE** in PioneerVFD. The theme connects to
`ws://127.0.0.1:17455` and the menu row switches to `HLPR`. Check the
"Don't ask again on this profile" box on the consent modal to skip it
on future sessions.

Or, if you'd rather drive it manually:

```sh
pvfd-hlpr
```

Leave the terminal running while you use PVFD. Stop with `Ctrl+C`.

Requirements: Python 3.10+, `parec`/`pactl` (from `libpulse` on Arch) or
`pw-record` (from `pipewire-audio` on Arch), `numpy`, `websockets`,
`rich`. The pip/pipx install pulls the Python deps; the audio tools are
yours to install via `pacman`.

## Probe mode

Before debugging "PULSE isn't pulsing," run:

```sh
pvfd-hlpr --probe
```

Shows three tables (capture tools, PipeWire sinks, active sink inputs)
with Spotify highlighted, plus a summary panel with the auto-detected
target. No WebSocket binds.

## Options

```
pvfd-hlpr --port 17455               # bind a different port
pvfd-hlpr --target <sink>.monitor    # pick a specific PipeWire monitor source
pvfd-hlpr --target sink-input:<id>   # record one playback stream by sink-input ID
pvfd-hlpr --smoothing 0.32           # AnalyserNode-style smoothing constant (0.0 = raw)
pvfd-hlpr --legacy-eq                # apply the 0.1.8 fixed treble tilt before dB
pvfd-hlpr --verbose                  # debug logging
pvfd-hlpr --stats                    # one capture stats line per second
pvfd-hlpr --probe                    # one-shot diagnostic, no WS bind
pvfd-hlpr --with-spotify             # one-shot: install the Spotify launcher hook
pvfd-hlpr --unlink-spotify           # remove the Spotify launcher hook
pvfd-hlpr --version
```

Auto-detection finds the sink Spotify is currently routed to (via
`pactl list sink-inputs`) and uses its `.monitor` source. Some Spotify/Linux
combinations expose the active stream only as `media.name="audio-src"`; when
that stream is the lone active sink-input, HLPR treats it as the Spotify
candidate. When `parec` is available, HLPR records that exact playback stream
with `parec --monitor-stream=<id>` so speakers/headphones output routing does
not change the capture target. HLPR asks `parec` for a 20 ms capture latency
and 10 ms process time so the WebSocket stream updates at PVFD's visualizer
cadence instead of arriving in large Pulse/PipeWire chunks. Falls back to the
sink monitor, then the default sink's monitor, when Spotify isn't playing.

While running, the helper re-probes the target every ~10 seconds and
respawns the capture subprocess if Spotify's sink-input changes (e.g.
Spotify just started, or you switched between speakers and headphones).

## Protocol (v1)

`ws://127.0.0.1:<port>` (default `17455`).

The helper only accepts WebSocket clients with Spotify's web origins
(`https://xpui.app.spotify.com`, `https://open.spotify.com`) or no Origin
header, which keeps unrelated browser pages from subscribing to localhost audio
data.

On connect, helper sends one text frame:

```json
{
  "type": "hello",
  "version": "0.1.12",
  "protocol": 1,
  "sampleRate": 48000,
  "fftSize": 2048,
  "binCount": 1024,
  "minDb": -100.0,
  "maxDb": -30.0,
  "spectrumProfile": "pvfd-chromium-v2.1"
}
```

Each subsequent frame is **1024 raw bytes**, one byte per FFT bin, following
the Web Audio
[`getByteFrequencyData`](https://developer.mozilla.org/en-US/docs/Web/API/AnalyserNode/getByteFrequencyData)
convention: `byte = clamp(((dB - minDb) / (maxDb - minDb)) * 255, 0, 255)`.
Frame rate is 30 Hz, matching PVFD's `LOGO_LIVE_AUDIO_SCHEDULER_MS = 33`.

The FFT pipeline mirrors Chromium's `AnalyserNode` so PVFD's bar visualizer
sees the same byte shape on Linux as it does on Windows/macOS native capture:

- **Blackman window** (Web Audio spec).
- **Exponential magnitude smoothing** with `smoothingTimeConstant = 0.32`,
  matching the actual `AnalyserNode` config in PVFD
  (`pioneerVFD.js`). This is what makes bars *glide* rather than *flicker*
  — the smoothing the analyser would normally do, applied here instead
  because PVFD bypasses `AnalyserNode` on Linux. Override with
  `--smoothing <τ>`; `0.0` is raw, `0.8` is the Web Audio default.
- **Flat dB→byte mapping** by default. PVFD's downstream band AGC and
  per-bar smoothing were tuned against a flat `AnalyserNode`, so the
  helper deliberately doesn't pre-EQ. Older 0.1.8 behavior (a fixed
  treble tilt) is still available with `--legacy-eq` for A/B testing.

Future protocol versions will bump the `protocol` integer. PVFD checks the
version on connect and surfaces an "update HLPR" notification on mismatch
rather than silently sending wrong data.

## Why this exists

Spotify's audio output on Linux goes to the PipeWire/Pulse graph, not into
the Chromium renderer's media element. That means:

- Picking Spotify in the Chromium screen-share picker returns a silent track
  even when the picker offers it (verified empirically — see
  [PVFD #16](https://github.com/adainstarks/PioneerVFD/issues/16)).
- The only working capture mechanism is to tap the OS audio graph directly.

xdg-desktop-portal *can* do this (via "Share system audio" on monitor
capture), but support varies by backend. KDE Wayland's
`xdg-desktop-portal-kde` gained reliable audio support only relatively
recently, and many users report missing checkboxes or empty pickers.

`pvfd-hlpr` does the same thing the portal would — `pw-record` against
Spotify's sink monitor — but with deterministic behavior and a debuggable
single binary instead of a portal black box.

## License

MIT.
