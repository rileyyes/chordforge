"""Test-only guitar-like audio synthesiser (additive plucks, strummed chords)."""
import numpy as np

SR = 22050

VOICINGS = {  # MIDI notes, open-position guitar voicings
    "Am": [45, 52, 57, 60, 64], "F": [41, 48, 53, 57, 60, 65], "C": [48, 52, 55, 60, 64],
    "G": [43, 47, 50, 55, 59, 67], "Em": [40, 47, 52, 55, 59, 64], "D": [50, 57, 62, 66],
    "Dm": [50, 57, 62, 65], "A": [45, 52, 57, 61, 64], "E": [40, 47, 52, 56, 59, 64],
    "Bb": [46, 53, 58, 62, 65], "Cm": [48, 55, 60, 63, 67],
}


def pluck(midi, dur, sr=SR, rng=None):
    f0 = 440.0 * 2 ** ((midi - 69) / 12.0)
    t = np.arange(int(dur * sr)) / sr
    y = np.zeros_like(t)
    for h in range(1, 9):
        if f0 * h > sr / 2 - 500:
            break
        decay = 2.0 + 1.2 * h
        y += (1.0 / h ** 1.1) * np.sin(2 * np.pi * f0 * h * (1 + 0.0002 * h * h) * t) * np.exp(-decay * t)
    y *= np.minimum(1.0, t / 0.004)
    return y


def strum(chord, dur, sr=SR, spread=0.012, detune_cents=0.0, gain=1.0):
    n = int(dur * sr)
    out = np.zeros(n + int(0.2 * sr))
    for i, m in enumerate(VOICINGS[chord]):
        p = pluck(m + detune_cents / 100.0, dur + 0.15, sr) * (0.8 + 0.05 * i)
        s = int(i * spread * sr)
        out[s:s + p.size] += p[: out.size - s]
    return out * gain


def progression_audio(chords, bpm=92, beats_per_chord=4, strums_per_beat=1, sr=SR, lead_silence=0.3,
                      tail=0.5, noise=0.0, detune_cents=0.0, seed=0):
    """Each chord lasts beats_per_chord beats, strummed strums_per_beat times per beat."""
    beat = 60.0 / bpm
    total = lead_silence + len(chords) * beats_per_chord * beat + tail
    y = np.zeros(int(total * sr) + sr)
    step = beat / strums_per_beat
    for ci, ch in enumerate(chords):
        for k in range(int(beats_per_chord * strums_per_beat)):
            t0 = lead_silence + ci * beats_per_chord * beat + k * step
            s = strum(ch, step * 1.6, sr, detune_cents=detune_cents, gain=1.0 if k % strums_per_beat == 0 else 0.7)
            i0 = int(t0 * sr)
            y[i0:i0 + s.size] += s[: y.size - i0]
    y = y[: int(total * sr)]
    if noise:
        y += np.random.default_rng(seed).normal(0, noise, y.size)
    return (y / (np.max(np.abs(y)) + 1e-9) * 0.7).astype(np.float32)
