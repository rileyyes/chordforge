import io
import unittest
from pathlib import Path
import wave

import numpy as np

from chordforge.audio.io import decode_audio, encode_wav, ffmpeg_available, sniff_format
from chordforge.config import Config
from chordforge.errors import AudioError, PayloadTooLarge
from tests.helpers import guitar_wav
from tests.synth import SR

CFG = Config(max_upload_bytes=2 * 1024 * 1024)


def tone_wav(seconds, sr=22050, amp=0.5, width=2, channels=1):
    t = np.arange(int(seconds * sr)) / sr
    y = amp * np.sin(2 * np.pi * 220 * t)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(sr)
        if width == 2:
            pcm = (y * 32767).astype("<i2")
        elif width == 1:
            pcm = ((y * 127) + 128).astype(np.uint8)
        else:
            pcm = (y * (2 ** 31 - 1)).astype("<i4")
        if channels == 2:
            pcm = np.repeat(pcm, 2)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


class TestAudioInput(unittest.TestCase):
    """decode_audio() handles format/size/duration/corruption. There is no separate "preprocess"
    step any more (chord/tempo/key analysis and its preprocessing were removed); the one remaining
    content check -- rejecting silent audio -- lives in services.py's _check_not_silent, which is
    exercised via clip-upload tests (see test_song_services.py) rather than here."""

    def test_valid_wav(self):
        a = decode_audio(guitar_wav(), CFG)
        self.assertEqual(a.format, "wav")
        self.assertGreater(a.duration, 5)

    def test_wav_variants(self):
        for width in (1, 2, 4):
            with self.subTest(width=width):
                self.assertAlmostEqual(decode_audio(tone_wav(3, width=width), CFG).duration, 3, places=1)
        self.assertAlmostEqual(decode_audio(tone_wav(3, channels=2), CFG).duration, 3, places=1)

    def test_empty(self):
        with self.assertRaises(AudioError) as cm:
            decode_audio(b"", CFG)
        self.assertEqual(cm.exception.code, "empty_audio")

    def test_wav_with_no_samples(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        with self.assertRaises(AudioError) as cm:
            decode_audio(buf.getvalue(), CFG)
        self.assertIn(cm.exception.code, ("empty_audio", "corrupt_audio"))

    def test_unsupported_type(self):
        for junk in (b"hello world this is text", b"\x89PNG\r\n\x1a\n" + b"0" * 100, b"MZ" + b"\x00" * 200):
            with self.subTest(junk=junk[:8]), self.assertRaises(AudioError) as cm:
                decode_audio(junk, CFG)
            self.assertEqual(cm.exception.code, "unsupported_format")

    def test_corrupt_wav(self):
        good = guitar_wav()
        for bad in (good[:30], b"RIFF" + b"\x00" * 4 + b"WAVE" + b"garbage" * 10):
            with self.subTest(len=len(bad)), self.assertRaises(AudioError):
                decode_audio(bad, CFG)

    def test_truncated_wav_data_still_decodes_what_exists(self):
        good = guitar_wav()
        a = decode_audio(good[: len(good) // 2], CFG)  # header claims more frames than present
        self.assertGreater(a.duration, 2)

    def test_oversized(self):
        with self.assertRaises(PayloadTooLarge):
            decode_audio(b"RIFF" + b"\x00" * (3 * 1024 * 1024), CFG)

    def test_too_short_and_too_long(self):
        with self.assertRaises(AudioError) as cm:
            decode_audio(tone_wav(0.5), CFG)
        self.assertEqual(cm.exception.code, "too_short")
        with self.assertRaises(AudioError) as cm:
            decode_audio(tone_wav(70, sr=8000), Config(max_upload_bytes=5 * 1024 * 1024))
        self.assertEqual(cm.exception.code, "too_long")

    def test_sniff(self):
        self.assertEqual(sniff_format(guitar_wav()), "wav")
        self.assertEqual(sniff_format(b"OggS" + b"\0" * 10), "ogg")
        self.assertEqual(sniff_format(b"fLaC" + b"\0" * 10), "flac")
        self.assertIsNone(sniff_format(b"plain"))

    @unittest.skipUnless(ffmpeg_available(), "ffmpeg not installed")
    def test_compressed_formats_via_ffmpeg(self):
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.wav")
            Path(src).write_bytes(guitar_wav())
            for ext, codec in (("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "libvorbis"), ("m4a", "aac")):
                out = os.path.join(d, "a." + ext)
                r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src, "-c:a", codec, out], capture_output=True)
                if r.returncode != 0:
                    continue
                with self.subTest(ext=ext):
                    a = decode_audio(Path(out).read_bytes(), CFG)
                    self.assertEqual(a.format, ext)
                    self.assertGreater(a.duration, 5)
            # a truncated/garbled mp3 must fail cleanly
            mp3 = os.path.join(d, "a.mp3")
            if os.path.exists(mp3):
                junk = b"ID3" + os.urandom(2000)
                with self.assertRaises(AudioError):
                    decode_audio(junk, CFG)


if __name__ == "__main__":
    unittest.main()
