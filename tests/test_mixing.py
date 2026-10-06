"""Tests for chordforge/mixing.py -- the fixed, real-DSP auto-mix chain that replaces generation.
No synthesis is exercised here: every function takes plain numpy arrays (in place of loaded clip
audio) and is checked for a measurable, correct effect."""
import unittest
import wave
from io import BytesIO

import numpy as np

from chordforge.mixing import (SR, MixdownEmptyError, auto_level, compress, crossfade_concat, duck,
                               envelope_follower, highpass, mix_section, mix_structure, peaking_eq,
                               soft_limit)
from chordforge.song import ClipPlacement, SectionSpec, SongStructureSpec


def tone(freq, seconds, sr=SR, amp=0.3):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float64)


class TestAutoLevel(unittest.TestCase):
    def test_quiet_signal_is_brought_up(self):
        x = tone(220, 1.0, amp=0.01)
        y = auto_level(x)
        self.assertGreater(rms(y), rms(x))

    def test_loud_signal_is_brought_down(self):
        x = tone(220, 1.0, amp=0.9)
        y = auto_level(x)
        self.assertLess(rms(y), rms(x))

    def test_silence_untouched(self):
        x = np.zeros(1000)
        self.assertTrue(np.array_equal(auto_level(x), x))

    def test_empty_array(self):
        self.assertEqual(auto_level(np.array([])).size, 0)

    def test_gain_is_clamped(self):
        # an extremely quiet signal shouldn't be amplified into something huge
        x = tone(220, 1.0, amp=1e-4)
        y = auto_level(x, min_gain=0.3, max_gain=4.0)
        self.assertLessEqual(np.max(np.abs(y)), np.max(np.abs(x)) * 4.0 + 1e-9)


def rms(x):
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if x.size else 0.0


class TestEnvelopeFollower(unittest.TestCase):
    def test_tracks_a_loud_then_quiet_signal(self):
        loud = tone(220, 0.5, amp=0.8)
        quiet = np.zeros(int(0.5 * SR))
        x = np.concatenate([loud, quiet])
        env = envelope_follower(x, SR, attack_ms=5.0, release_ms=50.0)
        self.assertEqual(env.size, x.size)
        self.assertGreater(env[int(0.3 * SR)], env[-1])

    def test_empty(self):
        self.assertEqual(envelope_follower(np.array([]), SR).size, 0)


class TestDuck(unittest.TestCase):
    def test_reduces_bed_when_vocal_present(self):
        bed = tone(110, 1.0, amp=0.5)
        vocal_env = np.ones(bed.size)  # vocal "fully present" throughout
        ducked = duck(bed, vocal_env, amount=0.55, floor=0.4)
        self.assertLess(rms(ducked), rms(bed))
        self.assertGreaterEqual(rms(ducked), rms(bed) * 0.4 - 1e-6)

    def test_no_reduction_when_vocal_absent(self):
        bed = tone(110, 1.0, amp=0.5)
        vocal_env = np.zeros(bed.size)
        ducked = duck(bed, vocal_env)
        self.assertTrue(np.allclose(ducked, bed))


class TestCompress(unittest.TestCase):
    def test_reduces_dynamic_range(self):
        loud = tone(220, 0.5, amp=0.9)
        quiet = tone(220, 0.5, amp=0.05)
        x = np.concatenate([loud, quiet])
        y = compress(x, SR, threshold_db=-18.0, ratio=4.0)
        ratio_before = rms(loud) / max(rms(quiet), 1e-9)
        ratio_after = rms(y[: loud.size]) / max(rms(y[loud.size:]), 1e-9)
        self.assertLess(ratio_after, ratio_before)


class TestFiltersAndCrossfade(unittest.TestCase):
    def test_highpass_removes_low_frequency_energy(self):
        low = tone(40, 1.0, amp=0.5)
        y = highpass(low, SR, 200.0)
        self.assertLess(rms(y), rms(low) * 0.5)

    def test_peaking_eq_noop_at_zero_gain(self):
        x = tone(500, 0.5)
        self.assertTrue(np.allclose(peaking_eq(x, SR, 1000.0, 0.0), x))

    def test_peaking_eq_boost_increases_energy_at_band(self):
        x = tone(1000, 0.5, amp=0.2)
        y = peaking_eq(x, SR, 1000.0, 6.0, q=1.0)
        self.assertGreater(rms(y), rms(x))

    def test_crossfade_concat_length_is_shorter_than_naive_concat(self):
        a, b = tone(220, 1.0), tone(330, 1.0)
        naive_len = a.size + b.size
        out = crossfade_concat([a, b], SR, xfade_ms=100.0)
        self.assertLess(out.size, naive_len)
        self.assertGreater(out.size, a.size)  # still roughly both chunks' worth

    def test_crossfade_concat_skips_empty_chunks(self):
        a = tone(220, 0.5)
        out = crossfade_concat([np.array([]), a, np.array([])], SR)
        self.assertTrue(np.allclose(out, a))

    def test_crossfade_concat_of_nothing(self):
        self.assertEqual(crossfade_concat([], SR).size, 0)


class TestSoftLimit(unittest.TestCase):
    def test_never_exceeds_target_peak(self):
        x = tone(220, 1.0, amp=5.0)  # way over 0dBFS
        y = soft_limit(x, target_peak=0.89)
        self.assertLessEqual(float(np.max(np.abs(y))), 0.891)

    def test_empty(self):
        self.assertEqual(soft_limit(np.array([])).size, 0)


class TestMixSection(unittest.TestCase):
    def test_empty_section_returns_none(self):
        self.assertIsNone(mix_section([], np.random.default_rng(0)))

    def test_vocal_and_bed_are_combined(self):
        vocal = ClipPlacement("v", "vocals", tone(440, 2.0, amp=0.3), gain=1.0, loop=False)
        guitar = ClipPlacement("g", "guitar", tone(220, 2.0, amp=0.3), gain=1.0, loop=False)
        out = mix_section([vocal, guitar], np.random.default_rng(0))
        self.assertIsNotNone(out)
        self.assertGreater(out.size, 0)
        self.assertGreater(float(np.max(np.abs(out))), 0.0)

    def test_section_length_is_longest_clip(self):
        short = ClipPlacement("s", "guitar", tone(220, 1.0), gain=1.0, loop=False)
        long = ClipPlacement("l", "vocals", tone(440, 3.0), gain=1.0, loop=False)
        out = mix_section([short, long], np.random.default_rng(0))
        self.assertAlmostEqual(out.size / SR, 3.0, delta=0.05)

    def test_looping_clip_fills_the_section(self):
        riff = ClipPlacement("r", "guitar", tone(220, 0.5), gain=1.0, loop=True)
        long_vocal = ClipPlacement("v", "vocals", tone(440, 3.0), gain=1.0, loop=False)
        out = mix_section([riff, long_vocal], np.random.default_rng(0))
        self.assertAlmostEqual(out.size / SR, 3.0, delta=0.05)


class TestMixStructure(unittest.TestCase):
    def test_raises_on_truly_empty_structure(self):
        structure = SongStructureSpec([SectionSpec("Intro", []), SectionSpec("Verse", [])])
        with self.assertRaises(MixdownEmptyError):
            mix_structure(structure, seed=1)

    def test_raises_on_no_sections(self):
        with self.assertRaises(MixdownEmptyError):
            mix_structure(SongStructureSpec([]), seed=1)

    def test_produces_valid_wav_for_a_simple_song(self):
        intro = SectionSpec("Intro", [ClipPlacement("g", "guitar", tone(220, 1.5), gain=1.0, loop=False)])
        verse = SectionSpec("Verse", [
            ClipPlacement("g2", "guitar", tone(220, 2.0), gain=1.0, loop=False),
            ClipPlacement("v", "vocals", tone(440, 2.0), gain=1.0, loop=False),
        ])
        wav_bytes, duration = mix_structure(SongStructureSpec([intro, verse]), seed=42)
        self.assertGreater(duration, 2.5)  # both sections, minus crossfade overlap
        with wave.open(BytesIO(wav_bytes)) as w:
            self.assertEqual(w.getframerate(), SR)
            self.assertGreater(w.getnframes(), 0)

    def test_deterministic_for_a_given_seed(self):
        def make():
            return SongStructureSpec([SectionSpec("V", [
                ClipPlacement("g", "guitar", tone(220, 0.5), gain=1.0, loop=True),
            ])])
        wav1, _ = mix_structure(make(), seed=7)
        wav2, _ = mix_structure(make(), seed=7)
        self.assertEqual(wav1, wav2)


if __name__ == "__main__":
    unittest.main()
