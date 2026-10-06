"""The auto-mix engine: turns your own recorded clips, arranged into song sections, into one
finished, polished track. This is real digital signal processing on your own audio -- there is no
synthesis and nothing here invents sound that wasn't in one of your recordings. What it does:

1. Auto-levels each clip (so a quiet phone-mic vocal and a loud DI guitar don't need manual
   gain-riding just to sit together).
2. Separates each section into a "vocal bus" (your vocals clips) and an "instrumental bed" (every
   other track type) and ducks the bed slightly whenever the vocal is present, so vocals sit on
   top instead of fighting the guitar for space.
3. Applies gentle, fixed-parameter EQ and compression to each bus (a high-pass to clear out
   rumble, a small EQ move to carve space, and light dynamic-range compression so levels don't
   swing wildly).
4. Crossfades between sections instead of hard-cutting.
5. Runs a final soft limiter so the master never clips, and normalises to a sensible peak.

Every stage is a small, independently testable function (see tests/test_mixing.py). None of it is
tunable by genre/mood -- it's the same honest signal-processing chain regardless of what you
recorded, which is why there's no "style" picker in the UI for this any more.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.signal import butter, lfilter, sosfilt

from .audio.io import encode_wav
from .song import ClipPlacement, SongStructureSpec

SR = 44100  # working/output sample rate for the mixer
VOCAL_TRACK_TYPES = {"vocals"}


class MixdownEmptyError(ValueError):
    """Raised when a song structure has nothing actually placed in it to mix. Caught by the job
    runner and reported to the user as a plain, safe message (see services.py)."""


# ---------------------------------------------------------------------------
# small DSP building blocks -- each one is a pure function over a 1-D float array
# ---------------------------------------------------------------------------

def _add(buf: np.ndarray, start: int, snd: np.ndarray, gain: float = 1.0) -> None:
    if start >= buf.size or start < 0 or snd.size == 0:
        return
    end = min(buf.size, start + snd.size)
    buf[start:end] += snd[: end - start] * gain


def _loop_to_length_humanized(seg: np.ndarray, n: int, xfade: int, rng: np.random.Generator) -> np.ndarray:
    """Tile `seg` to fill `n` samples with a short crossfade at each seam and a small random
    gain jitter (+/-5%) per repeat after the first, so a looped sustain doesn't sound like a
    mechanical, exact repeat."""
    if seg.size == 0 or n <= 0:
        return np.zeros(max(0, n))
    if seg.size >= n:
        return seg[:n].copy()
    xfade = min(xfade, seg.size // 3) if seg.size >= 3 else 0
    step = max(1, seg.size - xfade)
    out = np.zeros(n)
    pos, rep_i = 0, 0
    while pos < n:
        take = min(seg.size, n - pos)
        gain = 1.0 if rep_i == 0 else float(rng.uniform(0.95, 1.05))
        chunk = seg[:take] * gain
        if xfade and pos > 0:
            k = min(xfade, take)
            fade = np.linspace(0.0, 1.0, k)
            chunk[:k] = out[pos:pos + k] * (1 - fade) + chunk[:k] * fade
        out[pos:pos + take] = chunk
        pos += step
        rep_i += 1
    return out


def _loop_natural(seg: np.ndarray, n: int, sr: int, rng: np.random.Generator, xfade: int) -> np.ndarray:
    """Fill an n-sample slot from a single recorded clip `seg`. Plays the attack once, then loops
    just the ringing/sustain tail (with a crossfade and slight per-repeat volume variation)
    instead of repeating the whole clip back to back, which would sound like a stutering repeat
    rather than one continuous performance. Works for any recorded instrument or voice, not just
    guitar -- it just finds where the initial transient ends and loops what's after it."""
    if seg.size == 0 or n <= 0:
        return np.zeros(max(0, n))
    if n <= seg.size:
        out = seg[:n].copy()
    else:
        win = max(1, int(0.008 * sr))
        n_frames = seg.size // win
        attack_end = seg.size
        if n_frames >= 4:
            env = np.sqrt(np.array([np.mean(seg[i * win:(i + 1) * win] ** 2) for i in range(n_frames)]))
            peak_i = int(np.argmax(env))
            after = np.where(env[peak_i:] <= env[peak_i] * 0.55)[0]
            raw_end = (peak_i + (after[0] if after.size else env.size - peak_i)) * win
            min_head, max_head = int(0.02 * sr), max(int(0.02 * sr), seg.size - int(0.08 * sr))
            attack_end = int(min(max(raw_end, min_head), max_head))
        head = seg[:attack_end]
        sustain = seg[attack_end:] if attack_end < seg.size else seg
        k = min(xfade, head.size, max(1, sustain.size)) if head.size > 2 else 0
        tail = _loop_to_length_humanized(sustain, (n - head.size) + k, xfade, rng)
        if k > 2:
            fade = np.linspace(0.0, 1.0, k)
            seam = head[-k:] * (1 - fade) + tail[:k] * fade
            out = np.concatenate([head[:-k], seam, tail[k:]])
        else:
            out = np.concatenate([head, tail])
        out = out[:n] if out.size >= n else np.pad(out, (0, n - out.size))
    fade_n = min(out.size // 8, int(0.015 * sr))
    if fade_n > 1:
        out[:fade_n] *= np.linspace(0.0, 1.0, fade_n)
        out[-fade_n:] *= np.linspace(1.0, 0.0, fade_n)
    return out


def _place_once(seg: np.ndarray, n: int) -> np.ndarray:
    """Play `seg` once inside an n-sample slot (silence for the rest), with a short fade in/out
    to avoid clicks. Used when a clip's "loop" flag is off."""
    if n <= 0:
        return np.zeros(0)
    if seg.size == 0:
        return np.zeros(n)
    take = min(seg.size, n)
    out = np.zeros(n)
    out[:take] = seg[:take]
    fade_n = min(take // 8, int(0.015 * SR)) if take else 0
    if fade_n > 1:
        out[:fade_n] *= np.linspace(0.0, 1.0, fade_n)
        out[take - fade_n:take] *= np.linspace(1.0, 0.0, fade_n)
    return out


def auto_level(x: np.ndarray, target_rms: float = 0.18, min_gain: float = 0.3, max_gain: float = 4.0) -> np.ndarray:
    """Scale a clip so its RMS (average loudness) sits near a common target, clamped so a
    near-silent recording isn't blown up into blasting noise and an already-loud one isn't cut
    down to nothing. This is what lets a quiet phone-mic take and a hot DI guitar sit together
    without you manually riding faders first."""
    if x.size == 0:
        return x
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if rms < 1e-6:
        return x  # essentially silent -- leave it alone rather than amplify noise floor
    gain = float(np.clip(target_rms / rms, min_gain, max_gain))
    return x * gain


def envelope_follower(x: np.ndarray, sr: int, attack_ms: float = 10.0, release_ms: float = 150.0,
                       control_hz: float = 1000.0) -> np.ndarray:
    """A standard attack/release envelope follower: tracks the loudness of `x` over time, rising
    quickly (attack) and falling more slowly (release), which is what both the compressor and the
    ducking below use to decide how much to turn things down and when. Computed at a reduced
    "control rate" (control_hz) rather than sample-by-sample -- inaudible for gain-control purposes
    and orders of magnitude faster on a multi-minute track."""
    if x.size == 0:
        return x.copy()
    hop = max(1, int(round(sr / control_hz)))
    n_blocks = max(1, int(np.ceil(x.size / hop)))
    pad = n_blocks * hop - x.size
    xp = np.pad(np.abs(x), (0, pad)) if pad else np.abs(x)
    block_peak = xp.reshape(n_blocks, hop).max(axis=1)
    eff_hz = sr / hop
    ca = float(np.exp(-1.0 / (max(1e-6, attack_ms / 1000.0) * eff_hz)))
    cr = float(np.exp(-1.0 / (max(1e-6, release_ms / 1000.0) * eff_hz)))
    env = np.empty(n_blocks)
    prev = 0.0
    for i in range(n_blocks):
        v = block_peak[i]
        coeff = ca if v > prev else cr
        prev = coeff * prev + (1.0 - coeff) * v
        env[i] = prev
    return np.repeat(env, hop)[: x.size]


def duck(bed: np.ndarray, vocal_envelope_norm: np.ndarray, amount: float = 0.55, floor: float = 0.4) -> np.ndarray:
    """Turn `bed` down by up to `amount` wherever `vocal_envelope_norm` (0..1, how present the
    vocal is right now) is high, never below `floor` of its original level -- the instrumental
    bed recedes under the vocal instead of disappearing."""
    if bed.size == 0:
        return bed
    gain = 1.0 - amount * vocal_envelope_norm
    gain = np.maximum(gain, floor)
    return bed * gain


def compress(x: np.ndarray, sr: int, threshold_db: float = -18.0, ratio: float = 3.0,
             attack_ms: float = 10.0, release_ms: float = 120.0, makeup_db: float = 0.0) -> np.ndarray:
    """A basic feed-forward compressor: wherever the signal's envelope exceeds `threshold_db`,
    gain is reduced so the excess above threshold is divided by `ratio`. Keeps levels from
    swinging as wildly as a raw, unprocessed recording would."""
    if x.size == 0:
        return x
    env = envelope_follower(x, sr, attack_ms, release_ms)
    env_db = 20.0 * np.log10(np.maximum(env, 1e-9))
    over_db = np.maximum(0.0, env_db - threshold_db)
    gain_db = -over_db * (1.0 - 1.0 / ratio) + makeup_db
    return x * (10.0 ** (gain_db / 20.0))


def peaking_eq(x: np.ndarray, sr: int, freq: float, gain_db: float, q: float = 1.0) -> np.ndarray:
    """A peaking EQ band (boost or cut) centred at `freq`, using the standard RBJ biquad
    cookbook formula. gain_db > 0 boosts, < 0 cuts; 0 is a no-op."""
    if x.size == 0 or gain_db == 0:
        return x
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * freq / sr
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    b0, b1, b2 = 1 + alpha * a, -2 * cos_w0, 1 - alpha * a
    a0, a1, a2 = 1 + alpha / a, -2 * cos_w0, 1 - alpha / a
    b = np.array([b0, b1, b2]) / a0
    aa = np.array([a0, a1, a2]) / a0
    return lfilter(b, aa, x)


def highpass(x: np.ndarray, sr: int, freq: float, order: int = 2) -> np.ndarray:
    """A gentle high-pass filter, mainly to clear low-frequency rumble/handling noise."""
    if x.size == 0:
        return x
    sos = butter(order, freq, btype="highpass", fs=sr, output="sos")
    return sosfilt(sos, x)


def crossfade_concat(chunks: list, sr: int, xfade_ms: float = 120.0) -> np.ndarray:
    """Concatenate a list of audio chunks (one per song section) with a short equal-power
    crossfade at each boundary instead of a hard, audible cut between sections."""
    chunks = [c for c in chunks if c.size]
    if not chunks:
        return np.zeros(0)
    out = chunks[0].copy()
    xfade_n = int(sr * xfade_ms / 1000.0)
    for nxt in chunks[1:]:
        k = min(xfade_n, out.size, nxt.size)
        if k <= 1:
            out = np.concatenate([out, nxt])
            continue
        t = np.linspace(0.0, 1.0, k)
        fade_out, fade_in = np.cos(t * np.pi / 2.0), np.sin(t * np.pi / 2.0)
        seam = out[-k:] * fade_out + nxt[:k] * fade_in
        out = np.concatenate([out[:-k], seam, nxt[k:]])
    return out


def soft_limit(x: np.ndarray, target_peak: float = 0.89) -> np.ndarray:
    """A gentle soft-clip (tanh) limiter followed by peak normalisation, so the master never
    clips and ends up at a consistent, safe level."""
    if x.size == 0:
        return x
    y = np.tanh(1.3 * x) / np.tanh(1.3)
    peak = float(np.max(np.abs(y)))
    if peak > 1e-9:
        y = y / peak * target_peak
    return y


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------

def _section_length(placements: list) -> int:
    lens = [p.samples.size for p in placements if p.samples.size]
    return max(lens) if lens else 0


def _render_placement(p: ClipPlacement, n: int, rng: np.random.Generator) -> np.ndarray:
    if n <= 0 or p.samples.size == 0:
        return np.zeros(max(0, n))
    leveled = auto_level(p.samples)
    clip = _loop_natural(leveled, n, SR, rng, int(0.02 * SR)) if p.loop else _place_once(leveled, n)
    return clip * p.gain


def mix_section(placements: list, rng: np.random.Generator) -> Optional[np.ndarray]:
    """Render one section's placements into a finished stretch of audio: level each clip, split
    into a vocal bus and an instrumental bed, duck the bed under the vocal, then apply each bus's
    EQ/compression. Returns None for a section with nothing placed in it (skipped entirely)."""
    n = _section_length(placements)
    if n <= 0:
        return None
    vocal_bus, bed_bus = np.zeros(n), np.zeros(n)
    for p in placements:
        clip = _render_placement(p, n, rng)
        if p.track_type in VOCAL_TRACK_TYPES:
            vocal_bus += clip
        else:
            bed_bus += clip
    has_vocal, has_bed = bool(np.abs(vocal_bus).max(initial=0.0) > 1e-9), bool(np.abs(bed_bus).max(initial=0.0) > 1e-9)
    if has_vocal and has_bed:
        venv = envelope_follower(vocal_bus, SR, attack_ms=15.0, release_ms=250.0)
        ref = float(np.percentile(venv, 95)) if venv.size else 0.0
        vnorm = np.clip(venv / ref, 0.0, 1.0) if ref > 1e-9 else np.zeros_like(venv)
        bed_bus = duck(bed_bus, vnorm, amount=0.55, floor=0.4)
    if has_vocal:
        vocal_bus = highpass(vocal_bus, SR, 90.0)
        vocal_bus = peaking_eq(vocal_bus, SR, 3000.0, 2.5, q=1.0)
        vocal_bus = compress(vocal_bus, SR, threshold_db=-18.0, ratio=3.0)
    if has_bed:
        bed_bus = highpass(bed_bus, SR, 70.0)
        bed_bus = peaking_eq(bed_bus, SR, 400.0, -2.0, q=1.0)  # carve room for the vocal
        bed_bus = compress(bed_bus, SR, threshold_db=-20.0, ratio=2.0)
    return vocal_bus + bed_bus


def mix_structure(structure: SongStructureSpec, seed: Optional[int] = None) -> tuple[bytes, float]:
    """Build the finished song from a SongStructureSpec whose placements already carry real,
    loaded clip audio (see ProjectService._build_song_structure_spec in services.py). Raises
    MixdownEmptyError if nothing at all is placed anywhere. Returns (wav_bytes, duration_seconds).
    """
    rng = np.random.default_rng(seed if seed is not None else 0)
    chunks = []
    for section in structure.sections:
        rendered = mix_section(section.placements, rng)
        if rendered is not None:
            chunks.append(rendered)
    if not chunks:
        raise MixdownEmptyError("Your song is empty -- add at least one part to a section first.")
    mix = crossfade_concat(chunks, SR, xfade_ms=120.0)
    fade_n = int(min(mix.size, 1.5 * SR))
    if fade_n > 1:
        mix[-fade_n:] *= np.linspace(1.0, 0.0, fade_n)
    mix = soft_limit(mix, target_peak=0.89)
    wav = encode_wav(mix, SR)
    return wav, mix.size / SR
