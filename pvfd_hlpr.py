#!/usr/bin/env python3
"""PioneerVFD HLPR — Linux PipeWire audio bridge for the PVFD Spicetify theme.

Background
----------
On Linux, ``navigator.mediaDevices.getDisplayMedia()`` is mediated by
xdg-desktop-portal. Two real users (Arch + EndeavourOS / KDE Wayland) report
that the portal either (a) doesn't list Spotify or any usable source, or
(b) lists sources but omits the "Share system audio" checkbox. Even when
Spotify can be picked, the resulting audio track is silent because
Spotify's playback goes to the OS audio graph (PipeWire) rather than the
Chromium renderer's media element.

This helper bypasses the portal entirely: it taps Spotify's PipeWire output
through ``pw-record``, runs an FFT, and streams ``getByteFrequencyData``-shaped
frequency bins to PVFD over a localhost WebSocket. PVFD's theme JS includes
a small WebSocket client that feeds those bins into the existing analyser
pipeline.

Wire protocol (v1)
------------------
Endpoint: ``ws://127.0.0.1:17455`` (override with ``--port``).

On connect, helper sends one text frame::

    {
      "type": "hello",
      "version": "0.1.0",
      "protocol": 1,
      "sampleRate": 48000,
      "fftSize": 2048,
      "binCount": 1024,
      "minDb": -100.0,
      "maxDb": -30.0
    }

Then each frame: 1024 raw bytes, one byte per FFT bin. The byte value
follows the Web Audio ``getByteFrequencyData`` convention::

    byte = clamp(((dB - minDb) / (maxDb - minDb)) * 255, 0, 255)

Frame rate is 30 Hz to match PVFD's ``LOGO_LIVE_AUDIO_SCHEDULER_MS = 33``.

Requirements
------------
* Linux with PipeWire (``pw-record`` available — package
  ``pipewire-utils`` on Arch / ``pipewire-pulse`` on most distros).
* Python 3.10+.
* ``pip install websockets numpy`` (only for running from source; the
  released binary bundles these).

Usage
-----
::

    pvfd-hlpr                    # default: auto-detect Spotify monitor, port 17455
    pvfd-hlpr --probe            # one-shot: list PipeWire sinks/monitors and exit
    pvfd-hlpr --version
    pvfd-hlpr --port 17455
    pvfd-hlpr --target <name>    # force a specific monitor source
    pvfd-hlpr --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.stderr.write("pvfd-hlpr requires numpy. Install with: pip install numpy websockets\n")
    sys.exit(1)

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.stderr.write("pvfd-hlpr requires websockets. Install with: pip install websockets numpy\n")
    sys.exit(1)

# rich is preferred for CLI presentation but optional — fall back to plain
# logging/print so the helper still runs if it's somehow not installed.
try:
    from rich.console import Console
    from rich.logging import RichHandler
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    _RICH = True
    _console = Console()
except ImportError:  # pragma: no cover
    _RICH = False
    _console = None  # type: ignore[assignment]


__version__ = "0.1.12"
PROTOCOL_VERSION = 1
ALLOWED_ORIGINS = [
    None,
    "https://xpui.app.spotify.com",
    "https://open.spotify.com",
]

SAMPLE_RATE = 48000
CHANNELS = 2
FFT_SIZE = 2048
BIN_COUNT = FFT_SIZE // 2
HOP_SAMPLES = SAMPLE_RATE // 30  # ~33 ms per frame, matches PVFD scheduler
MIN_DB = -100.0
MAX_DB = -30.0
DB_RANGE = MAX_DB - MIN_DB
PAREC_LOW_LATENCY_ARGS = ["--latency-msec=20", "--process-time-msec=10"]

# Web Audio AnalyserNode uses a Blackman window (see W3C Web Audio spec §1.10.1).
_n = np.arange(FFT_SIZE, dtype=np.float32)
_BLACKMAN = (
    0.42
    - 0.5 * np.cos(2.0 * np.pi * _n / FFT_SIZE)
    + 0.08 * np.cos(4.0 * np.pi * _n / FFT_SIZE)
).astype(np.float32)
del _n
_MAG_NORM = float(FFT_SIZE)
_BIN_FREQS = (np.arange(BIN_COUNT, dtype=np.float32) * (SAMPLE_RATE / FFT_SIZE)).astype(np.float32)

# Matches pioneerVFD.js: AnalyserNode.smoothingTimeConstant = 0.32 (not the Web
# Audio spec default of 0.8). Applied to magnitude *before* dB. Override with
# --smoothing.
SMOOTHING_TIME_CONSTANT = 0.32

# Legacy fixed EQ tilt — kept behind --legacy-eq for A/B against 0.1.8. PVFD's
# bar visualizer was tuned against a flat AnalyserNode, so the default path now
# ships flat bytes too.
SPECTRUM_PROFILE = "pvfd-chromium-v2.1"
_VISUAL_EQ_HZ = np.array(
    [0, 28, 70, 160, 420, 1500, 3200, 7000, 12000, 20000, 24000],
    dtype=np.float32,
)
_VISUAL_EQ_DB_POINTS = np.array(
    [-18.0, -14.0, -10.0, -5.0, -1.0, 5.5, 10.0, 14.0, 17.0, 18.0, 18.0],
    dtype=np.float32,
)
_VISUAL_EQ_DB = np.interp(_BIN_FREQS, _VISUAL_EQ_HZ, _VISUAL_EQ_DB_POINTS).astype(np.float32)

logger = logging.getLogger("pvfd-hlpr")

# Pre-rendered figlet "small_slant" art for the startup panel. Kept as a literal
# so we don't take a runtime dependency on pyfiglet.
_BANNER_ART = (
    "   ___ _   _________    __ ________   ___  _______ \n"
    "  / _ \\ | / / __/ _ \\  / // / __/ /  / _ \\/ __/ _ \\\n"
    " / ___/ |/ / _// // / / _  / _// /__/ ___/ _// , _/\n"
    "/_/   |___/_/ /____/ /_//_/___/____/_/  /___/_/|_| "
)


def _configure_logging(verbose: bool = False) -> None:
    """Set up logging — RichHandler when rich is available, plain otherwise."""
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    # Wipe any prior handlers so re-entry (tests, reloads) doesn't double-format.
    for h in list(root.handlers):
        root.removeHandler(h)
    if _RICH and _console is not None:
        handler: logging.Handler = RichHandler(
            console=_console,
            rich_tracebacks=True,
            show_path=False,
            show_time=True,
            log_time_format="%H:%M:%S",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    logger.setLevel(level)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def _render_startup_panel(port: int, target: Optional[str]) -> None:
    """Print the hero banner + status block. Falls back to plain logger.info."""
    if not _RICH or _console is None:
        logger.info(
            "pvfd-hlpr %s — listening on ws://127.0.0.1:%d (protocol v%d)",
            __version__, port, PROTOCOL_VERSION,
        )
        logger.info("target: %s | profile: %s", target or "(default)", SPECTRUM_PROFILE)
        return
    body = Text()
    body.append(_BANNER_ART, style="bright_magenta")
    body.append("\n\n")
    body.append("version  ", style="dim")
    body.append(f"{__version__}", style="bold")
    body.append(f"  ·  protocol v{PROTOCOL_VERSION}", style="dim")
    body.append("\n")
    body.append("listen   ", style="dim")
    body.append(f"ws://127.0.0.1:{port}", style="bold cyan")
    body.append("\n")
    body.append("target   ", style="dim")
    body.append(f"{target or '(default monitor)'}", style="bold")
    body.append("\n")
    body.append("profile  ", style="dim")
    body.append(SPECTRUM_PROFILE, style="bold")
    _console.print(Panel(body, title="[bold bright_magenta]pvfd-hlpr[/]",
                         border_style="bright_magenta", expand=False, padding=(1, 2)))


# ---------- target detection ----------

def list_pactl_sinks() -> list[dict[str, str]]:
    """Return a list of {id, name, monitor_source, description} for each
    PulseAudio/PipeWire sink visible via pactl. Empty list if pactl is missing
    or the call fails."""
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(
            ["pactl", "list", "sinks"],
            check=True, capture_output=True, text=True, timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("pactl list sinks failed: %s", exc)
        return []
    sinks = []
    for block in out.split("Sink #")[1:]:
        m_id = re.match(r"\s*(\d+)", block)
        m_name = re.search(r"^\s*Name:\s*(\S+)", block, re.MULTILINE)
        m_monitor = re.search(r"^\s*Monitor Source:\s*(\S+)", block, re.MULTILINE)
        m_desc = re.search(r"^\s*Description:\s*(.+)$", block, re.MULTILINE)
        if not (m_id and m_name):
            continue
        sinks.append({
            "id": m_id.group(1),
            "name": m_name.group(1),
            "monitor": (m_monitor.group(1) if m_monitor else f"{m_name.group(1)}.monitor"),
            "description": (m_desc.group(1).strip() if m_desc else ""),
        })
    return sinks


def list_pactl_sink_inputs() -> list[dict[str, str]]:
    """Return active playback streams visible via pactl."""
    if not shutil.which("pactl"):
        return []
    try:
        out = subprocess.run(
            ["pactl", "list", "sink-inputs"],
            check=True, capture_output=True, text=True, timeout=4,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("pactl list sink-inputs failed: %s", exc)
        return []
    inputs = []
    for block in out.split("Sink Input #")[1:]:
        m_id = re.match(r"\s*(\d+)", block)
        m_sink = re.search(r"^\s*Sink:\s*(\d+)", block, re.MULTILINE)
        if not (m_id and m_sink):
            continue
        props = {
            "id": m_id.group(1),
            "sink": m_sink.group(1),
            "media.name": "",
            "application.name": "",
            "application.process.binary": "",
            "application.process.command_line": "",
            "node.name": "",
            "node.description": "",
        }
        for key in list(props.keys())[2:]:
            match = re.search(r'^\s*' + re.escape(key) + r'\s*=\s*"?(.*?)"?\s*$', block, re.MULTILINE)
            if match:
                props[key] = match.group(1).strip()
        inputs.append(props)
    return inputs


def find_spotify_sink_input() -> Optional[dict[str, str]]:
    """Return the active Spotify-like sink input, if one can be identified."""
    inputs = list_pactl_sink_inputs()
    for item in inputs:
        haystack = " ".join([
            item.get("media.name", ""),
            item.get("application.name", ""),
            item.get("application.process.binary", ""),
            item.get("application.process.command_line", ""),
            item.get("node.name", ""),
            item.get("node.description", ""),
        ]).lower()
        if "spotify" in haystack:
            return item
    audio_src_inputs = [
        item for item in inputs
        if item.get("media.name", "").strip().lower() == "audio-src"
    ]
    if len(audio_src_inputs) == 1:
        logger.info(
            "using lone audio-src sink-input #%s as Spotify candidate",
            audio_src_inputs[0]["id"],
        )
        return audio_src_inputs[0]
    if len(inputs) == 1:
        logger.info(
            "using only active sink-input #%s (%s) as Spotify candidate",
            inputs[0]["id"],
            inputs[0].get("media.name") or "unnamed",
        )
        return inputs[0]
    return None


def find_spotify_sink_id() -> Optional[str]:
    """Return the Sink ID Spotify is currently playing into."""
    item = find_spotify_sink_input()
    return item["sink"] if item else None


def find_default_monitor() -> Optional[str]:
    if not shutil.which("pactl"):
        return None
    try:
        default_sink = subprocess.run(
            ["pactl", "get-default-sink"],
            check=True, capture_output=True, text=True, timeout=4,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return f"{default_sink}.monitor" if default_sink else None


def auto_detect_target() -> Optional[str]:
    """Return the best guess for which monitor source to record from."""
    spotify_input = find_spotify_sink_input()
    if spotify_input and shutil.which("parec"):
        return f"sink-input:{spotify_input['id']}"
    spotify_sink_id = spotify_input["sink"] if spotify_input else None
    sinks = list_pactl_sinks()
    if spotify_sink_id is not None:
        for sink in sinks:
            if sink["id"] == spotify_sink_id:
                return sink["monitor"]
    return find_default_monitor()


# ---------- capture subprocess ----------

async def spawn_pw_record(target: Optional[str]) -> asyncio.subprocess.Process:
    if target and target.startswith("sink-input:") and shutil.which("parec"):
        stream_id = target.split(":", 1)[1]
        cmd = [
            "parec",
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16le",
            "--raw",
            f"--monitor-stream={stream_id}",
            *PAREC_LOW_LATENCY_ARGS,
        ]
    elif target and target.startswith("sink-input:"):
        raise RuntimeError(
            "sink-input capture requires parec — install libpulse on Arch"
        )
    else:
        use_parec_monitor = bool(target and target.endswith(".monitor") and shutil.which("parec"))
        if use_parec_monitor:
            cmd = [
                "parec",
                "--rate", str(SAMPLE_RATE),
                "--channels", str(CHANNELS),
                "--format", "s16le",
                "--raw",
                "-d", target,
                *PAREC_LOW_LATENCY_ARGS,
            ]
        elif shutil.which("pw-record"):
            cmd = [
                "pw-record",
                "--rate", str(SAMPLE_RATE),
                "--channels", str(CHANNELS),
                "--format", "s16",
            ]
            if target:
                cmd += ["--target", target]
            cmd += ["-"]
        elif shutil.which("parec"):
            cmd = [
                "parec",
                "--rate", str(SAMPLE_RATE),
                "--channels", str(CHANNELS),
                "--format", "s16le",
                "--raw",
                *PAREC_LOW_LATENCY_ARGS,
            ]
            if target:
                cmd += ["-d", target]
        else:
            raise RuntimeError(
                "neither pw-record nor parec found on PATH — install pipewire-utils "
                "(Arch) or pulseaudio-utils"
            )
    logger.info("starting capture: %s", " ".join(cmd))
    popen_kwargs = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **popen_kwargs,
    )


class FrameProducer:
    """Captures audio, computes FFT bins, fans frames out to all WS clients."""

    def __init__(
        self,
        target: Optional[str],
        stats: bool = False,
        legacy_eq: bool = False,
        smoothing: float = SMOOTHING_TIME_CONSTANT,
        target_explicit: bool = False,
    ):
        self.target = target
        self.stats = stats
        self.legacy_eq = legacy_eq
        self.smoothing = max(0.0, min(0.99, float(smoothing)))
        self.target_explicit = target_explicit
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._consumers: list[asyncio.Queue[bytes]] = []
        self._task: Optional[asyncio.Task[None]] = None
        self._stopping = asyncio.Event()
        self._smoothed_mag = np.zeros(BIN_COUNT, dtype=np.float32)
        self._last_stats_at = 0.0
        self._last_frame_at = 0.0
        self._stats_frame_count = 0
        self._stats_max_gap_ms = 0.0

    def subscribe(self) -> asyncio.Queue[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._consumers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        if q in self._consumers:
            self._consumers.remove(q)

    async def start(self) -> None:
        if self._task is not None:
            return
        # Eager first spawn so callers see RuntimeError immediately if backend missing.
        self._proc = await spawn_pw_record(self.target)
        self._task = asyncio.create_task(self._supervisor())

    async def _supervisor(self) -> None:
        """Outer loop: runs _run, restarts on natural EOF or target change."""
        backoff_s = 0.5
        try:
            while not self._stopping.is_set():
                if self._proc is None:
                    try:
                        self._proc = await spawn_pw_record(self.target)
                    except RuntimeError as exc:
                        logger.error("respawn failed: %s", exc)
                        try:
                            await asyncio.wait_for(self._stopping.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                        continue
                try:
                    await self._run()
                finally:
                    if self._proc and self._proc.returncode is None:
                        try:
                            self._proc.terminate()
                        except ProcessLookupError:
                            pass
                        try:
                            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            try:
                                self._proc.kill()
                            except ProcessLookupError:
                                pass
                    self._proc = None
                if self._stopping.is_set():
                    break
                # Try re-detecting target before next spawn.
                if not self.target_explicit:
                    new_target = auto_detect_target()
                    if new_target and new_target != self.target:
                        logger.info("target changed: %s → %s", self.target, new_target)
                        self.target = new_target
                        backoff_s = 0.5
                        continue
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=backoff_s)
                except asyncio.TimeoutError:
                    pass
                backoff_s = min(backoff_s * 2.0, 5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("supervisor crashed")

    async def stop(self) -> None:
        self._stopping.set()
        for q in list(self._consumers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    logger.warning("capture process did not exit after kill")
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._proc = None
        self._task = None

    async def _read_chunk(self, n_bytes: int) -> Optional[bytes]:
        assert self._proc and self._proc.stdout
        buf = bytearray()
        while len(buf) < n_bytes:
            chunk = await self._proc.stdout.read(n_bytes - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    async def _run(self) -> None:
        assert self._proc is not None
        bytes_per_frame = FFT_SIZE * CHANNELS * 2
        hop_bytes = HOP_SAMPLES * CHANNELS * 2
        ring = bytearray()
        last_target_check = time.monotonic()
        try:
            while not self._stopping.is_set():
                # Periodic target re-detection — handles "Spotify launched after helper" and "user switched output device".
                if not self.target_explicit:
                    now = time.monotonic()
                    if now - last_target_check >= 10.0:
                        last_target_check = now
                        new_target = auto_detect_target()
                        if new_target and new_target != self.target:
                            logger.info("target changed: %s → %s", self.target, new_target)
                            self.target = new_target
                            return  # supervisor respawns with new target
                chunk = await self._read_chunk(hop_bytes)
                if chunk is None:
                    err = ""
                    if self._proc.stderr:
                        try:
                            err = (await self._proc.stderr.read(4096)).decode(errors="replace")
                        except Exception:
                            pass
                    logger.warning("capture stream ended (stderr: %s)", err.strip() or "<empty>")
                    return
                ring.extend(chunk)
                if len(ring) < bytes_per_frame:
                    continue
                window_bytes = ring[-bytes_per_frame:]
                del ring[:-bytes_per_frame]
                samples = np.frombuffer(window_bytes, dtype=np.int16).astype(np.float32)
                samples = samples.reshape(-1, CHANNELS).mean(axis=1) / 32768.0
                pcm_peak = float(np.max(np.abs(samples))) if samples.size else 0.0
                pcm_rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
                samples *= _BLACKMAN
                spectrum = np.fft.rfft(samples)[:BIN_COUNT]
                mag = np.abs(spectrum) / _MAG_NORM
                # Exponential smoothing on magnitude — matches AnalyserNode.
                tau = self.smoothing
                self._smoothed_mag = tau * self._smoothed_mag + (1.0 - tau) * mag
                db = 20.0 * np.log10(np.maximum(self._smoothed_mag, 1e-10))
                if self.legacy_eq:
                    db += _VISUAL_EQ_DB
                norm = np.clip((db - MIN_DB) / DB_RANGE, 0.0, 1.0)
                frame = (norm * 255.0).astype(np.uint8)
                if self.stats:
                    now = time.monotonic()
                    if self._last_frame_at:
                        gap_ms = (now - self._last_frame_at) * 1000.0
                        if gap_ms > self._stats_max_gap_ms:
                            self._stats_max_gap_ms = gap_ms
                    self._last_frame_at = now
                    self._stats_frame_count += 1
                    if now - self._last_stats_at >= 1.0:
                        elapsed = now - self._last_stats_at if self._last_stats_at else 1.0
                        fps = self._stats_frame_count / max(0.001, elapsed)
                        self._last_stats_at = now
                        band_means = []
                        for lo_hz, hi_hz in ((28, 70), (70, 160), (160, 420), (420, 1500), (1500, 3200), (3200, 7000), (7000, 12000)):
                            lo = max(1, int(lo_hz / (SAMPLE_RATE / FFT_SIZE)))
                            hi = min(BIN_COUNT - 1, int(np.ceil(hi_hz / (SAMPLE_RATE / FFT_SIZE))))
                            band_means.append(float(np.mean(frame[lo:hi + 1])) if hi >= lo else 0.0)
                        logger.info(
                            "capture stats: consumers=%d fps=%.1f max_gap_ms=%.1f pcm_peak=%.4f pcm_rms=%.4f bin_max=%d bands=%s",
                            len(self._consumers),
                            fps,
                            self._stats_max_gap_ms,
                            pcm_peak,
                            pcm_rms,
                            int(frame.max()) if frame.size else 0,
                            "[" + ", ".join(f"{v:.1f}" for v in band_means) + "]",
                        )
                        self._stats_frame_count = 0
                        self._stats_max_gap_ms = 0.0
                bins = frame.tobytes()
                if not self._consumers:
                    continue
                for q in list(self._consumers):
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        q.put_nowait(bins)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("FrameProducer loop crashed")


# ---------- WebSocket server ----------

HELLO_PAYLOAD = json.dumps({
    "type": "hello",
    "version": __version__,
    "protocol": PROTOCOL_VERSION,
    "sampleRate": SAMPLE_RATE,
    "fftSize": FFT_SIZE,
    "binCount": BIN_COUNT,
    "minDb": MIN_DB,
    "maxDb": MAX_DB,
    "spectrumProfile": SPECTRUM_PROFILE,
})


async def serve_client(producer: FrameProducer, ws: Any) -> None:
    peer = ws.remote_address
    logger.info("client connected: %s", peer)
    queue = producer.subscribe()
    try:
        await ws.send(HELLO_PAYLOAD)
        while True:
            frame = await queue.get()
            if frame is None:
                break
            await ws.send(frame)
    except websockets.ConnectionClosed:
        pass
    finally:
        producer.unsubscribe(queue)
        logger.info("client disconnected: %s", peer)


# ---------- probe mode ----------

def _probe_plain() -> int:
    """Fallback probe output when rich isn't available."""
    print(f"pvfd-hlpr {__version__} — probe mode\n")
    pw_record = shutil.which("pw-record")
    parec = shutil.which("parec")
    pactl = shutil.which("pactl")
    print("Capture tools:")
    print(f"  pw-record: {pw_record or '(not found)'}")
    print(f"  parec:     {parec or '(not found)'}")
    print(f"  pactl:     {pactl or '(not found)'}")
    print(f"\nSpectrum profile: {SPECTRUM_PROFILE}")
    if not (pw_record or parec):
        print("\nFATAL: no capture backend found. Install pipewire-utils (Arch) or pulseaudio-utils.")
        return 2
    sinks = list_pactl_sinks()
    print(f"\nSinks ({len(sinks)}):")
    for sink in sinks:
        print(f"  [{sink['id']}] {sink['name']}")
        if sink["description"]:
            print(f"        description: {sink['description']}")
        print(f"        monitor:     {sink['monitor']}")
    sink_inputs = list_pactl_sink_inputs()
    print(f"\nSink inputs ({len(sink_inputs)}):")
    for item in sink_inputs:
        label = (
            item.get("application.name")
            or item.get("media.name")
            or item.get("node.name")
            or "(unnamed)"
        )
        print(f"  [{item['id']}] sink #{item['sink']}  {label}")
        for key in ("media.name", "application.name", "application.process.binary", "node.name", "node.description"):
            value = item.get(key)
            if value:
                print(f"        {key}: {value}")
    spotify_sink_id = find_spotify_sink_id()
    print()
    if spotify_sink_id is not None:
        print(f"Spotify is currently routed to sink #{spotify_sink_id}.")
    else:
        print("Spotify isn't currently playing (no sink-input matching application.name=Spotify).")
    default_monitor = find_default_monitor()
    if default_monitor:
        print(f"Default sink's monitor: {default_monitor}")
    auto = auto_detect_target()
    print(f"\nAuto-detected target for capture: {auto or '(none — pw-record will use its default)'}")
    return 0


def cmd_probe() -> int:
    if not _RICH or _console is None:
        return _probe_plain()

    _console.print()
    _render_startup_panel(int(os.environ.get("PVFD_HLPR_PORT", 17455)), None)

    pw_record = shutil.which("pw-record")
    parec = shutil.which("parec")
    pactl = shutil.which("pactl")

    def _tool_status(path: Optional[str]) -> Text:
        if path:
            return Text(path, style="green")
        return Text("(not found)", style="red")

    tools = Table(title="Capture tools", title_style="bold bright_magenta",
                  show_header=True, header_style="bold", border_style="bright_black")
    tools.add_column("Tool", style="bold")
    tools.add_column("Path")
    tools.add_row("pw-record", _tool_status(pw_record))
    tools.add_row("parec", _tool_status(parec))
    tools.add_row("pactl", _tool_status(pactl))
    _console.print(tools)

    if not (pw_record or parec):
        _console.print("[bold red]FATAL:[/] no capture backend found. Install pipewire-utils (Arch) or pulseaudio-utils.")
        return 2

    sinks = list_pactl_sinks()
    sinks_table = Table(title=f"Sinks ({len(sinks)})", title_style="bold bright_magenta",
                        show_header=True, header_style="bold", border_style="bright_black")
    sinks_table.add_column("ID", style="dim")
    sinks_table.add_column("Name", style="bold")
    sinks_table.add_column("Description")
    sinks_table.add_column("Monitor", style="cyan")
    for sink in sinks:
        sinks_table.add_row(
            sink["id"],
            sink["name"],
            sink.get("description") or "—",
            sink["monitor"],
        )
    _console.print(sinks_table)

    sink_inputs = list_pactl_sink_inputs()
    inputs_table = Table(title=f"Sink inputs ({len(sink_inputs)})", title_style="bold bright_magenta",
                         show_header=True, header_style="bold", border_style="bright_black")
    inputs_table.add_column("ID", style="dim")
    inputs_table.add_column("Sink", style="dim")
    inputs_table.add_column("Label", style="bold")
    inputs_table.add_column("Binary / Node")
    for item in sink_inputs:
        label = (
            item.get("application.name")
            or item.get("media.name")
            or item.get("node.name")
            or "(unnamed)"
        )
        binary = item.get("application.process.binary") or item.get("node.name") or "—"
        is_spotify = "spotify" in (binary + " " + label).lower()
        label_text = Text(label, style="bright_green" if is_spotify else "bold")
        inputs_table.add_row(item["id"], f"#{item['sink']}", label_text, binary)
    _console.print(inputs_table)

    spotify_sink_id = find_spotify_sink_id()
    default_monitor = find_default_monitor()
    auto = auto_detect_target()

    summary = Text()
    summary.append("Spotify routing  ", style="dim")
    if spotify_sink_id is not None:
        summary.append(f"sink #{spotify_sink_id}", style="bold bright_green")
    else:
        summary.append("(not playing)", style="yellow")
    summary.append("\n")
    summary.append("Default monitor  ", style="dim")
    summary.append(default_monitor or "(unknown)", style="bold")
    summary.append("\n")
    summary.append("Auto target      ", style="dim")
    summary.append(auto or "(pw-record default)", style="bold cyan")
    _console.print(Panel(summary, title="[bold bright_magenta]Summary[/]",
                         border_style="bright_magenta", expand=False, padding=(1, 2)))
    return 0


# ---------- spotify launcher hook ----------

PVFD_HLPR_DESKTOP_MARKER = "# pvfd-hlpr-wrapped"


def _spotify_desktop_candidates() -> list[Path]:
    home = Path.home()
    return [
        # AUR `spotify` and most native installs
        Path("/usr/share/applications/spotify.desktop"),
        Path("/usr/local/share/applications/spotify.desktop"),
        # `spotify-launcher` (Arch official `extra` repo)
        Path("/usr/share/applications/spotify-launcher.desktop"),
        Path("/usr/local/share/applications/spotify-launcher.desktop"),
        # Flatpak
        Path("/var/lib/flatpak/exports/share/applications/com.spotify.Client.desktop"),
        home / ".local/share/flatpak/exports/share/applications/com.spotify.Client.desktop",
        # Snap
        Path("/var/lib/snapd/desktop/applications/spotify_spotify.desktop"),
        # User overrides (last so we prefer pristine system sources for the unwrapped Exec)
        home / ".local/share/applications/spotify.desktop",
        home / ".local/share/applications/spotify-launcher.desktop",
    ]


def _find_spotify_desktop_source() -> Optional[Path]:
    user_dir = Path.home() / ".local/share/applications"
    user_overrides = {
        user_dir / "spotify.desktop",
        user_dir / "spotify-launcher.desktop",
    }
    for cand in _spotify_desktop_candidates():
        if cand in user_overrides:
            continue
        if cand.exists():
            return cand
    for cand in user_overrides:
        if cand.exists():
            return cand
    return None


def _wrap_exec_line(line: str, hlpr_path: str) -> str:
    prefix, sep, value = line.partition("=")
    if sep != "=" or not value.strip():
        return line
    # Escape single quotes for the sh -c wrapper.
    safe_hlpr = hlpr_path.replace("'", "'\\''")
    safe_original = value.strip().replace("'", "'\\''")
    wrapped = (
        f"sh -c 'pgrep -x pvfd-hlpr >/dev/null || "
        f"{safe_hlpr} --exit-with-spotify >/dev/null 2>&1 & "
        f"exec {safe_original}'"
    )
    return f"{prefix}={wrapped}"


def cmd_link_to_spotify() -> int:
    source = _find_spotify_desktop_source()
    if source is None:
        print("Couldn't find Spotify's launcher entry. Is Spotify installed?")
        return 2

    hlpr_path = shutil.which("pvfd-hlpr") or "pvfd-hlpr"

    try:
        content = source.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Could not read {source}: {exc}")
        return 2

    new_lines: list[str] = []
    changed = False
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        suffix = line[len(stripped):]
        if stripped.startswith("Exec=") and "pvfd-hlpr" not in stripped:
            new_lines.append(_wrap_exec_line(stripped, hlpr_path) + suffix)
            changed = True
        else:
            new_lines.append(line)
    if not changed:
        print(f"No Exec= lines to wrap in {source}.")
        return 2

    new_content = "".join(new_lines)
    if not new_content.endswith("\n"):
        new_content += "\n"
    new_content += f"{PVFD_HLPR_DESKTOP_MARKER}\n"

    user_dir = Path.home() / ".local/share/applications"
    dest = user_dir / source.name
    try:
        user_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.read_text(encoding="utf-8") == new_content:
            print(f"✓ Spotify launcher link already up to date ({dest}).")
            return 0
        dest.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        print(f"Could not write {dest}: {exc}")
        return 2

    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(
                ["update-desktop-database", str(user_dir)],
                check=False, capture_output=True, timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    print("✓ Linked pvfd-hlpr to Spotify launches.")
    print(f"  Override:   {dest}")
    print(f"  Source:     {source}")
    print("  From now on, launching Spotify (KDE menu, dock, `spotify` in terminal) starts pvfd-hlpr too.")
    print("  To undo:    pvfd-hlpr --unlink-spotify")
    return 0


def cmd_unlink_spotify() -> int:
    user_dir = Path.home() / ".local/share/applications"
    candidates = [
        user_dir / "spotify.desktop",
        user_dir / "spotify-launcher.desktop",
        user_dir / "com.spotify.Client.desktop",
        user_dir / "spotify_spotify.desktop",
    ]
    removed: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if PVFD_HLPR_DESKTOP_MARKER not in text and "--exit-with-spotify" not in text:
            # Not ours — don't touch a hand-edited override.
            continue
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            print(f"Could not remove {path}: {exc}")

    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(
                ["update-desktop-database", str(user_dir)],
                check=False, capture_output=True, timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    if removed:
        print("✓ Unlinked. Removed:")
        for p in removed:
            print(f"  {p}")
    else:
        print("No pvfd-hlpr → Spotify link found (nothing to remove).")
    return 0


async def _watch_spotify(stop_event: asyncio.Event) -> None:
    """Poll for Spotify-related processes every 2s. When all are gone, set stop_event.

    Tracks both `spotify` (the real client) and `spotify-launcher` (the Arch
    `extra` wrapper that fetches+execs the real client). A 30s grace period
    lets spotify-launcher bootstrap before we'd ever decide Spotify is gone.
    """
    if not shutil.which("pgrep"):
        logger.warning("--exit-with-spotify: pgrep not available, watcher disabled")
        return
    grace_until = time.monotonic() + 30.0
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        alive = False
        for name in ("spotify", "spotify-launcher"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-x", name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                rc = await proc.wait()
            except (OSError, asyncio.CancelledError):
                return
            if rc == 0:
                alive = True
                break
        if alive:
            # Reset grace; once we've seen Spotify alive, future absences mean it exited.
            grace_until = 0.0
            continue
        if time.monotonic() < grace_until:
            continue
        logger.info("Spotify exited — shutting down helper")
        stop_event.set()
        return


# ---------- main ----------

async def main_async(args: argparse.Namespace) -> int:
    _configure_logging(verbose=args.verbose)

    target = args.target
    if not target:
        target = auto_detect_target()
        if target:
            logger.info("auto-detected target: %s", target)
        else:
            logger.warning("could not auto-detect a target — pw-record will pick its default")

    producer = FrameProducer(
        target=target,
        stats=args.stats,
        legacy_eq=args.legacy_eq,
        smoothing=args.smoothing,
        target_explicit=bool(args.target),
    )
    try:
        await producer.start()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), stop_event.set)
        except (NotImplementedError, AttributeError):
            pass

    spotify_watcher_task: Optional[asyncio.Task[None]] = None
    if getattr(args, "exit_with_spotify", False):
        spotify_watcher_task = asyncio.create_task(_watch_spotify(stop_event))

    async def handler(ws: Any, _path: str = "/") -> None:
        await serve_client(producer, ws)

    _render_startup_panel(args.port, target)
    server = await websockets.serve(
        handler,
        "127.0.0.1",
        args.port,
        max_size=None,
        origins=ALLOWED_ORIGINS,
    )
    try:
        await stop_event.wait()
    finally:
        logger.info("shutting down")
        server.close()
        await producer.stop()
        if spotify_watcher_task is not None:
            spotify_watcher_task.cancel()
            try:
                await spotify_watcher_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("WebSocket server did not close within timeout")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pvfd-hlpr",
        description="PioneerVFD Linux audio helper — streams FFT bins to the PVFD theme over a localhost WebSocket.",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("PVFD_HLPR_PORT", 17455)))
    parser.add_argument("--target", type=str, default=None,
                        help="PipeWire/Pulse monitor source (e.g. <sink>.monitor). Auto-detected if omitted.")
    parser.add_argument("--probe", action="store_true",
                        help="List PipeWire sinks/monitors and exit without binding the WS.")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--stats", action="store_true",
                        help="Log one capture-level stats line per second while running.")
    parser.add_argument("--legacy-eq", action="store_true",
                        help="Apply the 0.1.8 fixed EQ tilt before dB mapping (A/B against flat bytes).")
    parser.add_argument("--smoothing", type=float, default=SMOOTHING_TIME_CONSTANT,
                        help="Magnitude smoothing constant (0.0=raw, 0.32=PVFD AnalyserNode default, 0.8=Web Audio spec default).")
    parser.add_argument("--with-spotify", dest="with_spotify", action="store_true",
                        help="One-shot setup: wrap Spotify's launcher so every Spotify launch also fires pvfd-hlpr, then exit.")
    parser.add_argument("--unlink-spotify", dest="unlink_spotify", action="store_true",
                        help="Remove the Spotify launcher link installed by --with-spotify.")
    parser.add_argument("--exit-with-spotify", dest="exit_with_spotify", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"pvfd-hlpr {__version__} (protocol v{PROTOCOL_VERSION})")
    args = parser.parse_args()
    if args.with_spotify:
        return cmd_link_to_spotify()
    if args.unlink_spotify:
        return cmd_unlink_spotify()
    if args.probe:
        return cmd_probe()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
