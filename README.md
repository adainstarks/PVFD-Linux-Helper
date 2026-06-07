# pvfd-hlpr

Linux audio helper for the [PioneerVFD](https://github.com/adainstarks/PioneerVFD)
Spicetify theme. Taps Spotify's PipeWire output, runs an FFT, and streams the
result to PVFD over a localhost WebSocket so the **PULSE** visualizer works
on Linux setups where Chromium's `getDisplayMedia()` picker doesn't list
Spotify or doesn't offer the "Share system audio" checkbox
(see PVFD [issue #16](https://github.com/adainstarks/PioneerVFD/issues/16)).

## Quickstart

Download the prebuilt binary from
[Releases](https://github.com/adainstarks/PVFD-Linux-Helper/releases/latest),
make it executable, and run it:

```sh
chmod +x pvfd-hlpr-linux-x86_64
./pvfd-hlpr-linux-x86_64
```

Then click **PULSE** in PioneerVFD. The theme will connect automatically to
`ws://127.0.0.1:17455` and the menu row will switch to `HLPR`.

Leave the terminal running while you use PVFD. Stop with `Ctrl+C`.

## Install from source

```sh
pipx install git+https://github.com/adainstarks/PVFD-Linux-Helper.git
# or
pip install --user git+https://github.com/adainstarks/PVFD-Linux-Helper.git
```

Requirements: Python 3.10+, `parec`/`pactl` (from `libpulse` on Arch) or
`pw-record` (from `pipewire-audio` on Arch), `numpy`, `websockets`.

## Probe mode

Before debugging "PULSE isn't pulsing," run:

```sh
pvfd-hlpr --probe
```

This lists detected sinks/monitors, shows whether Spotify is currently
routed somewhere, and prints the auto-detected target. No WebSocket binds.

## Options

```
pvfd-hlpr --port 17455               # bind a different port
pvfd-hlpr --target <sink>.monitor    # pick a specific PipeWire monitor source
pvfd-hlpr --verbose                  # debug logging
pvfd-hlpr --stats                    # one capture stats line per second
pvfd-hlpr --probe                    # one-shot diagnostic, no WS bind
pvfd-hlpr --version
```

Auto-detection finds the sink Spotify is currently routed to (via
`pactl list sink-inputs`) and uses its `.monitor` source. Some Spotify/Linux
combinations expose the active stream only as `media.name="audio-src"`; when
that stream is the lone active sink-input, HLPR treats it as the Spotify
candidate. Falls back to the default sink's monitor when Spotify isn't
playing. For Pulse/PipeWire monitor names (`*.monitor`), HLPR records through
`parec -d <monitor>` so the selected monitor source is captured directly.

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
  "version": "0.1.1",
  "protocol": 1,
  "sampleRate": 48000,
  "fftSize": 2048,
  "binCount": 1024,
  "minDb": -100.0,
  "maxDb": -30.0,
  "spectrumProfile": "pvfd-chromium-v1"
}
```

Each subsequent frame is **1024 raw bytes**, one byte per FFT bin, following
the Web Audio
[`getByteFrequencyData`](https://developer.mozilla.org/en-US/docs/Web/API/AnalyserNode/getByteFrequencyData)
convention: `byte = clamp(((dB - minDb) / (maxDb - minDb)) * 255, 0, 255)`.
Before that byte mapping, `pvfd-hlpr` applies the `pvfd-chromium-v1`
visualizer EQ profile so PipeWire's raw FFT spectrum lands closer to the
Chromium analyser shape PVFD was tuned against. Frame rate is 30 Hz.

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
