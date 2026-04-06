"""
gui/session_sounds.py

Lightweight notification tones for RF session connect / disconnect events.
Generates short sine-wave WAV data in-memory (no external files needed)
and plays via platform-native tools.

  Windows — winsound.PlaySound (in-memory WAV)
  macOS   — afplay via temp file
  Linux   — aplay  via temp file

All playback is non-blocking (fires in a daemon thread).
"""

import io
import math
import wave
import struct
import logging
import threading

log = logging.getLogger(__name__)

# ── Tone parameters ──────────────────────────────────────────────────────────

SAMPLE_RATE = 22050

# Connect tone:  ascending two-note chirp  (800 Hz → 1200 Hz, ~250 ms)
# Disconnect tone: descending single note  (600 Hz, ~200 ms)

def _generate_tone(frequency: float, duration_ms: int, volume: float = 0.5,
                   fade_ms: int = 15) -> bytes:
    """Generate a mono 16-bit PCM sine wave with fade-in/out to avoid clicks."""
    n_samples = int(SAMPLE_RATE * duration_ms / 1000)
    fade_samples = int(SAMPLE_RATE * fade_ms / 1000)
    samples = []
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        val = math.sin(2 * math.pi * frequency * t) * volume
        # Fade envelope
        if i < fade_samples:
            val *= i / fade_samples
        elif i > n_samples - fade_samples:
            val *= (n_samples - i) / fade_samples
        samples.append(int(val * 32767))
    return struct.pack(f"<{len(samples)}h", *samples)


def _build_wav(pcm_data: bytes) -> bytes:
    """Wrap raw PCM samples in a WAV container (in-memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return buf.getvalue()


def _connect_wav(volume: float) -> bytes:
    """Two-note ascending chirp: 800 Hz (120 ms) + 1200 Hz (130 ms)."""
    pcm = _generate_tone(800, 120, volume) + _generate_tone(1200, 130, volume)
    return _build_wav(pcm)


def _disconnect_wav(volume: float) -> bytes:
    """Single descending note: 600 Hz (200 ms)."""
    pcm = _generate_tone(600, 200, volume)
    return _build_wav(pcm)


# ── Playback ─────────────────────────────────────────────────────────────────

def _play_wav_bytes(wav_data: bytes):
    """Play WAV bytes using the best available platform method."""
    import sys

    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(wav_data,
                               winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            return
        except Exception as e:
            log.debug(f"winsound error: {e}")
            return

    # macOS / Linux — write to temp file, play with afplay / aplay
    import tempfile, subprocess, os
    suffix = ".wav"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(wav_data)
            tmp_path = tmp.name

        if sys.platform == "darwin":
            subprocess.run(["afplay", tmp_path], timeout=5,
                           capture_output=True)
        else:
            subprocess.run(["aplay", "-q", tmp_path], timeout=5,
                           capture_output=True)
    except FileNotFoundError:
        log.debug("No audio player found (afplay / aplay)")
    except Exception as e:
        log.debug(f"Session sound playback error: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ── Public API ───────────────────────────────────────────────────────────────

def play_connect_sound(volume: float = 0.5):
    """Play the session-connected notification tone (non-blocking)."""
    def _worker():
        try:
            wav = _connect_wav(volume)
            _play_wav_bytes(wav)
        except Exception as e:
            log.debug(f"Connect sound error: {e}")
    threading.Thread(target=_worker, daemon=True, name="SndConnect").start()


def play_disconnect_sound(volume: float = 0.5):
    """Play the session-disconnected notification tone (non-blocking)."""
    def _worker():
        try:
            wav = _disconnect_wav(volume)
            _play_wav_bytes(wav)
        except Exception as e:
            log.debug(f"Disconnect sound error: {e}")
    threading.Thread(target=_worker, daemon=True, name="SndDisconnect").start()
