"""Application services: the project/clip/song-structure workflow, and the mixdown job runner.

ChordForge is a recording and editing tool, not a generator: everything a project produces comes
from clips the user actually recorded or uploaded. The one piece of automation is the mixdown
("Build my song"), which assembles the clips placed into song sections and runs them through a
real, fixed digital-signal-processing chain (see chordforge/mixing.py) -- auto-leveling, ducking
the instrumental bed under vocals, gentle compression/EQ, section crossfades, and a final limiter.
Nothing here invents audio or fakes a result: mixing a truly empty song is refused with a clear
error rather than silently producing something.
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import numpy as np
from scipy.signal import resample_poly

from .audio.io import decode_audio, sniff_format
from .config import Config
from .db import Database, ClipRepo, ProjectRepo, SessionRepo, UserRepo, loads
from .errors import AppError, AudioError, Conflict, NotFound, ValidationError
from .mixing import SR as MIX_SR, MixdownEmptyError, mix_structure
from .song import ClipPlacement, SectionSpec, SongStructureSpec, TRACK_TYPES, TRACK_LABELS, validate_structure
from .storage import LocalStorage, Storage, make_key

log = logging.getLogger("chordforge")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
AUDIO_TYPES = {"wav": "audio/wav", "mp3": "audio/mpeg", "ogg": "audio/ogg", "flac": "audio/flac",
               "m4a": "audio/mp4", "webm": "audio/webm"}
SILENCE_PEAK_THRESHOLD = 1e-3
MAX_CLIP_LABEL = 60
MAX_CLIPS_PER_PROJECT = 30


def clean_name(name) -> str:
    if not isinstance(name, str):
        raise ValidationError("Project name must be text.")
    n = _CTRL.sub("", name).strip()
    if not 1 <= len(n) <= 80:
        raise ValidationError("Project name must be 1-80 characters.")
    return n


def _check_not_silent(audio) -> None:
    peak = float(np.max(np.abs(audio.samples))) if audio.samples.size else 0.0
    if peak < SILENCE_PEAK_THRESHOLD:
        raise AudioError("No audible signal was found in the recording. Check your input level.", "silent_audio")


def view(p: dict) -> dict:
    """Serialise a project row for the API. Storage keys are never exposed."""
    return {
        "id": p["id"], "name": p["name"], "created_at": p["created_at"], "updated_at": p["updated_at"],
        "mixdown": {"status": p["generation_status"], "error": p["generation_error"],
                    "attempts": p["generation_attempts"], "duration": p["generated_duration"],
                    "has_audio": bool(p["generated_key"]), "updated_at": p.get("generation_updated_at")},
    }


def clip_view(c: dict) -> dict:
    """Serialise a clip row for the API. Storage keys are never exposed."""
    return {
        "id": c["id"], "track_type": c["track_type"], "label": c["label"],
        "duration": c["duration"], "trim_start": c["trim_start"], "trim_end": c["trim_end"],
        "gain": c["gain"], "muted": bool(c["muted"]), "position": c["position"],
        "created_at": c["created_at"], "updated_at": c["updated_at"],
    }


class JobRunner:
    """Runs a mixdown in the background so the HTTP request that starts it returns immediately;
    the client polls project status the same way the old generation flow worked. `mixer` is
    injectable (a plain function taking (structure, seed) -> (wav_bytes, duration_seconds)) purely
    so tests can simulate a slow or failing mix without waiting on real DSP work."""

    def __init__(self, cfg: Config, projects: ProjectRepo, storage: Storage,
                 mixer: Callable[[SongStructureSpec, int], tuple[bytes, float]] = mix_structure):
        self.cfg, self.projects, self.storage, self.mixer = cfg, projects, storage, mixer
        self.pool = ThreadPoolExecutor(max_workers=cfg.mixdown_workers, thread_name_prefix="mix")

    def submit(self, user_id: str, pid: str, structure: SongStructureSpec, seed: int) -> None:
        self.pool.submit(self._run, user_id, pid, structure, seed)

    def _run(self, user_id: str, pid: str, structure: SongStructureSpec, seed: int) -> None:
        new_key = None
        try:
            self.projects.update(user_id, pid, generation_status="running", generation_error=None)
            wav_bytes, duration = self.mixer(structure, seed)
            new_key = make_key(user_id, pid, "gen", "wav")
            self.storage.put(new_key, wav_bytes)
            old = self.projects.get(user_id, pid)["generated_key"]
            self.projects.update(user_id, pid, generated_key=new_key, generated_duration=duration,
                                 generation_status="succeeded", generation_error=None)
            if old:
                try:
                    self.storage.delete(old)
                except AppError:
                    log.warning("could not delete old mixdown file for project %s", pid)
        except NotFound:
            if new_key:  # project deleted mid-mix
                try:
                    self.storage.delete(new_key)
                except AppError:
                    pass
        except MixdownEmptyError as e:
            self._fail(user_id, pid, str(e), new_key)
        except AppError as e:
            self._fail(user_id, pid, e.message if e.status < 500 else
                       "Couldn't build your song. Please try again.", new_key)
            log.warning("mixdown failed for project %s: %s (%s)", pid, e.code, e.message)
        except Exception:
            log.exception("unexpected mixdown error for project %s", pid)
            self._fail(user_id, pid, "Something went wrong building your song. Please try again.", new_key)

    def _fail(self, user_id, pid, message, new_key):
        if new_key:
            try:
                self.storage.delete(new_key)
            except AppError:
                pass
        try:
            self.projects.update(user_id, pid, generation_status="failed", generation_error=message)
        except (NotFound, Exception):
            pass


class ProjectService:
    def __init__(self, cfg: Config, projects: ProjectRepo, clips: ClipRepo, storage: Storage, runner: JobRunner):
        self.cfg, self.projects, self.clips, self.storage, self.runner = cfg, projects, clips, storage, runner
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, pid: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(pid, threading.Lock())

    # ---- CRUD ----
    def create(self, user_id: str, name=None) -> dict:
        if self.projects.count(user_id) >= self.cfg.max_projects_per_user:
            raise Conflict("Project limit reached. Delete an old project first.", "project_limit")
        n = clean_name(name) if name is not None else "Untitled song"
        return view(self.projects.create(user_id, n))

    def list(self, user_id: str) -> list[dict]:
        return [view(p) for p in self.projects.list(user_id)]

    def get(self, user_id: str, pid: str) -> dict:
        return view(self.projects.get(user_id, pid))

    def update(self, user_id: str, pid: str, patch: dict) -> dict:
        if not isinstance(patch, dict) or set(patch) - {"name"}:
            raise ValidationError("Invalid project update.")
        proj = self.projects.get(user_id, pid)
        fields: dict = {}
        if "name" in patch:
            fields["name"] = clean_name(patch["name"])
        return view(self.projects.update(user_id, pid, **fields)) if fields else view(proj)

    def delete(self, user_id: str, pid: str) -> None:
        with self._lock(pid):
            proj = self.projects.delete(user_id, pid)
        self.storage.delete_project(user_id, proj["id"])

    # ---- clips (your own recordings/uploads) ----
    def upload_clip(self, user_id: str, pid: str, track_type, label, data: bytes) -> dict:
        proj = self.projects.get(user_id, pid)  # 404s if not owned; also confirms project exists
        if not isinstance(track_type, str) or track_type not in TRACK_TYPES:
            raise ValidationError("Invalid track type. Choose one of: " + ", ".join(TRACK_TYPES) + ".")
        label = clean_name(label) if label else TRACK_LABELS[track_type]
        if len(label) > MAX_CLIP_LABEL:
            raise ValidationError(f"Clip name must be under {MAX_CLIP_LABEL} characters.")
        if len(self.clips.list(user_id, pid)) >= MAX_CLIPS_PER_PROJECT:
            raise Conflict(f"Limit of {MAX_CLIPS_PER_PROJECT} clips per project reached. Delete one first.",
                           "clip_limit")
        audio = decode_audio(data, self.cfg)  # validates size/type/duration/corruption
        _check_not_silent(audio)
        key = make_key(user_id, pid, "clip", sniff_format(data) or "wav")
        self.storage.put(key, data)
        try:
            row = self.clips.create(user_id, pid, track_type, label, key, audio.format, audio.duration)
        except Exception:
            try:
                self.storage.delete(key)
            except AppError:
                pass
            raise
        return clip_view(row)

    def list_clips(self, user_id: str, pid: str) -> list[dict]:
        self.projects.get(user_id, pid)
        return [clip_view(c) for c in self.clips.list(user_id, pid)]

    def update_clip(self, user_id: str, pid: str, clip_id: str, patch: dict) -> dict:
        self.projects.get(user_id, pid)
        allowed = {"label", "track_type", "trim_start", "trim_end", "gain", "muted", "position"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise ValidationError("Invalid clip update.")
        clip = self.clips.get(user_id, pid, clip_id)
        fields: dict = {}
        if "label" in patch:
            fields["label"] = clean_name(patch["label"])
            if len(fields["label"]) > MAX_CLIP_LABEL:
                raise ValidationError(f"Clip name must be under {MAX_CLIP_LABEL} characters.")
        if "track_type" in patch:
            if patch["track_type"] not in TRACK_TYPES:
                raise ValidationError("Invalid track type.")
            fields["track_type"] = patch["track_type"]
        trim_start = patch.get("trim_start", clip["trim_start"])
        trim_end = patch.get("trim_end", clip["trim_end"])
        if "trim_start" in patch or "trim_end" in patch:
            if (isinstance(trim_start, bool) or isinstance(trim_end, bool)
                    or not isinstance(trim_start, (int, float)) or not isinstance(trim_end, (int, float))):
                raise ValidationError("Trim points must be numbers.")
            if not 0 <= trim_start < trim_end <= clip["duration"] + 0.05:
                raise ValidationError("Trim start must be before trim end, within the clip's length.")
            fields["trim_start"], fields["trim_end"] = float(trim_start), float(min(trim_end, clip["duration"]))
        if "gain" in patch:
            g = patch["gain"]
            if isinstance(g, bool) or not isinstance(g, (int, float)) or not 0.1 <= g <= 3.0:
                raise ValidationError("Gain must be between 0.1 and 3.0.")
            fields["gain"] = float(g)
        if "muted" in patch:
            fields["muted"] = bool(patch["muted"])
        if "position" in patch:
            pos = patch["position"]
            if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
                raise ValidationError("Invalid position.")
            fields["position"] = pos
        return clip_view(self.clips.update(user_id, pid, clip_id, **fields)) if fields else clip_view(clip)

    def delete_clip(self, user_id: str, pid: str, clip_id: str) -> None:
        self.projects.get(user_id, pid)
        clip = self.clips.delete(user_id, pid, clip_id)
        try:
            self.storage.delete(clip["storage_key"])
        except AppError:
            log.warning("could not delete storage file for clip %s", clip_id)
        # Remove the clip from any song-structure section that referenced it, so a stale
        # reference can't linger (and so validate_structure never sees an unknown id again).
        proj = self.projects.get(user_id, pid)
        structure = loads(proj["song_structure_json"])
        if structure:
            changed = False
            for sec in structure["sections"]:
                before = len(sec["clips"])
                sec["clips"] = [c for c in sec["clips"] if c["clip_id"] != clip_id]
                changed = changed or len(sec["clips"]) != before
            if changed:
                self.projects.update(user_id, pid, song_structure_json=json.dumps(structure))

    def open_clip_audio(self, user_id: str, pid: str, clip_id: str) -> tuple[bytes, str]:
        self.projects.get(user_id, pid)
        clip = self.clips.get(user_id, pid, clip_id)
        data = self.storage.get(clip["storage_key"])
        ext = clip["storage_key"].rsplit(".", 1)[-1]
        return data, AUDIO_TYPES.get(ext, "application/octet-stream")

    # ---- song structure (arrangement of clips into sections) ----
    def get_structure(self, user_id: str, pid: str) -> dict:
        proj = self.projects.get(user_id, pid)
        structure = loads(proj["song_structure_json"])
        return structure or {"sections": []}

    def set_structure(self, user_id: str, pid: str, raw: dict) -> dict:
        proj = self.projects.get(user_id, pid)
        known_ids = {c["id"] for c in self.clips.list(user_id, pid)}
        structure = validate_structure(raw, known_ids)
        self.projects.update(user_id, pid, song_structure_json=json.dumps(structure))
        return structure

    def _build_song_structure_spec(self, user_id: str, pid: str, proj: dict) -> Optional[SongStructureSpec]:
        """Load real audio for every clip referenced by the project's song structure, trimmed and
        resampled to the mixer's sample rate. Returns None if there's no structure at all, so
        callers can tell "nothing set up yet" apart from "set up but empty"."""
        raw = loads(proj["song_structure_json"])
        if not raw or not raw.get("sections"):
            return None
        clips_by_id = {c["id"]: c for c in self.clips.list(user_id, pid)}
        cache: dict = {}

        def load_trimmed(clip_id: str) -> np.ndarray:
            if clip_id in cache:
                return cache[clip_id]
            c = clips_by_id.get(clip_id)
            if c is None:
                cache[clip_id] = np.array([])
                return cache[clip_id]
            try:
                audio = decode_audio(self.storage.get(c["storage_key"]), self.cfg)
            except AppError:
                log.warning("could not load clip %s for project %s", clip_id, pid)
                cache[clip_id] = np.array([])
                return cache[clip_id]
            x = np.asarray(audio.samples, dtype=np.float64)
            a, b = int(c["trim_start"] * audio.sr), int(c["trim_end"] * audio.sr)
            a, b = max(0, min(a, x.size)), max(0, min(b, x.size))
            x = x[a:b]
            if audio.sr != MIX_SR and x.size:
                g = math.gcd(int(audio.sr), MIX_SR)
                x = resample_poly(x, MIX_SR // g, int(audio.sr) // g)
            cache[clip_id] = x
            return x

        sections = []
        for sec in raw["sections"]:
            placements = []
            for item in sec["clips"]:
                c = clips_by_id.get(item["clip_id"])
                if c is None or c["muted"]:
                    continue
                seg = load_trimmed(item["clip_id"])
                if seg.size == 0:
                    continue
                placements.append(ClipPlacement(c["id"], c["track_type"], seg, gain=c["gain"], loop=item["loop"]))
            sections.append(SectionSpec(sec["name"], placements))
        return SongStructureSpec(sections)

    # ---- mixdown ("Build my song") ----
    def start_mixdown(self, user_id: str, pid: str) -> dict:
        with self._lock(pid):
            proj = self.projects.get(user_id, pid)
            if proj["generation_status"] in ("queued", "running"):
                raise Conflict("Your song is already being built.", "busy")
            structure = self._build_song_structure_spec(user_id, pid, proj)
            if structure is None or not any(sec.placements for sec in structure.sections):
                raise ValidationError("Add at least one part to a section before building your song.")
            seed = zlib.crc32(f"{pid}:{proj['generation_attempts']}".encode())
            p = self.projects.update(user_id, pid, generation_status="queued", generation_error=None,
                                     generation_attempts=proj["generation_attempts"] + 1)
        self.runner.submit(user_id, pid, structure, seed)
        return view(p)

    def open_mixdown_audio(self, user_id: str, pid: str) -> tuple[bytes, str, dict]:
        proj = self.projects.get(user_id, pid)
        if not proj["generated_key"]:
            raise NotFound("File not found.")
        data = self.storage.get(proj["generated_key"])
        ext = proj["generated_key"].rsplit(".", 1)[-1]
        return data, AUDIO_TYPES.get(ext, "application/octet-stream"), proj


def build_services(cfg: Config, mixer=None):
    db = Database(cfg.db_path)
    storage = LocalStorage(cfg.storage_dir)
    users, sessions, projects, clips = UserRepo(db), SessionRepo(db), ProjectRepo(db), ClipRepo(db)
    projects.fail_interrupted()
    sessions.purge_expired()
    runner = JobRunner(cfg, projects, storage, mixer=mixer or mix_structure)
    svc = ProjectService(cfg, projects, clips, storage, runner)
    return db, users, sessions, projects, storage, svc
