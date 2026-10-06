"""Audio input: validate and decode uploads. Never trusts the filename or Content-Type."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass

import numpy as np

from ..config import Config
from ..errors import AudioError, PayloadTooLarge

ALLOWED_FORMATS = ("wav", "mp3", "ogg", "flac", "m4a", "webm")
FFMPEG_SR = 22050


@dataclass
class DecodedAudio:
    samples: np.ndarray  # mono float32 in [-1, 1]
    sr: int
    format: str
    duration: float


def sniff_format(data: bytes) -> str | None:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            nch, width, sr, nframes = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if nch < 1 or nch > 8 or sr < 4000 or sr > 192000:
                raise AudioError("Unsupported WAV parameters.", "corrupt_audio")
            raw = w.readframes(nframes)
    except (wave.Error, EOFError, ValueError, OverflowError):
        raise AudioError("This WAV file is corrupt or uses an unsupported encoding.", "corrupt_audio")
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw[: len(raw) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
        v = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        v = np.where(v >= 1 << 23, v - (1 << 24), v)
        x = v.astype(np.float32) / float(1 << 23)
    elif width == 4:
        x = np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise AudioError("Unsupported WAV sample width.", "corrupt_audio")
    if x.size < nch:
        raise AudioError("The recording contains no audio.", "empty_audio")
    x = x[: x.size // nch * nch].reshape(-1, nch).mean(axis=1)
    return x.astype(np.float32), sr


def _decode_ffmpeg(data: bytes, ext: str, max_seconds: float) -> tuple[np.ndarray, int]:
    if not ffmpeg_available():
        raise AudioError("This audio format needs ffmpeg on the server. Please upload a WAV file.",
                         "unsupported_format")
    fd, path = tempfile.mkstemp(suffix="." + ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-t", str(max_seconds + 1.0),
               "-vn", "-ac", "1", "-ar", str(FFMPEG_SR), "-f", "f32le", "pipe:1"]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            raise AudioError("Decoding the audio took too long.", "corrupt_audio")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if p.returncode != 0 or not p.stdout:
        raise AudioError("This audio file could not be decoded. It may be corrupt.", "corrupt_audio")
    x = np.frombuffer(p.stdout[: len(p.stdout) // 4 * 4], dtype="<f4").copy()
    return x, FFMPEG_SR


def decode_audio(data: bytes, cfg: Config) -> DecodedAudio:
    """Validate an uploaded file and decode it to mono float samples.

    Raises AudioError / PayloadTooLarge with user-safe messages.
    """
    if not data:
        raise AudioError("The file is empty.", "empty_audio")
    if len(data) > cfg.max_upload_bytes:
        raise PayloadTooLarge(f"File is too large (limit {cfg.max_upload_bytes // (1024 * 1024)} MB).")
    fmt = sniff_format(data)
    if fmt is None or fmt not in ALLOWED_FORMATS:
        raise AudioError("Unsupported file type. Use WAV, MP3, OGG, FLAC, M4A or WebM audio.",
                         "unsupported_format")
    if fmt == "wav":
        try:
            x, sr = _decode_wav(data)
        except AudioError as e:
            if e.code == "corrupt_audio" and ffmpeg_available():
                x, sr = _decode_ffmpeg(data, "wav", cfg.max_duration_s)  # e.g. float WAV
            else:
                raise
    else:
        x, sr = _decode_ffmpeg(data, fmt, cfg.max_duration_s)
    if not np.all(np.isfinite(x)):
        raise AudioError("The audio contains invalid samples.", "corrupt_audio")
    duration = x.size / float(sr)
    if duration <= 0:
        raise AudioError("The recording contains no audio.", "empty_audio")
    if duration < cfg.min_duration_s:
        raise AudioError(f"Recording is too short ({duration:.1f}s). Play at least "
                         f"{cfg.min_duration_s:.0f} seconds.", "too_short")
    if duration > cfg.max_duration_s:
        raise AudioError(f"Recording is too long ({duration:.0f}s). The limit is "
                         f"{cfg.max_duration_s:.0f} seconds.", "too_long")
    return DecodedAudio(x, sr, fmt, duration)


def encode_wav(samples: np.ndarray, sr: int) -> bytes:
    """Encode mono float samples as 16-bit PCM WAV."""
    x = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    pcm = (x * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()
