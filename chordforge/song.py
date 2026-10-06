"""Song-structure types: an ordered list of sections (Intro/Verse/Chorus/...), each holding the
clips (your own recordings) that play in it. A section's length is not chosen by you -- it is
simply the length of the longest clip placed in it ("fit to your longest recording"), since there
is no chord-cycle generation left to size sections against. This module is intentionally pure data
+ validation; chordforge/mixing.py does the actual audio work once clip audio is loaded (see
ProjectService._build_song_structure_spec in services.py, which loads the real audio and builds
the SongStructureSpec this module describes).

Storage shape (song_structure_json on the project row), after validate_structure():
{
  "sections": [
    {"id": "s1", "name": "Intro", "clips": [{"clip_id": "...", "loop": true}, ...]},
    ...
  ]
}
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

import numpy as np

from .errors import ValidationError

TRACK_TYPES = ("vocals", "guitar", "drums", "bass", "keys", "lead", "other")
TRACK_LABELS = {"vocals": "Vocals", "guitar": "Guitar", "drums": "Drums", "bass": "Bass",
                "keys": "Keys", "lead": "Lead Guitar", "other": "Other"}
MAX_SECTIONS = 24
MAX_SECTION_NAME = 30
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class ClipPlacement:
    clip_id: str
    track_type: str
    samples: np.ndarray  # trimmed, mono, resampled to the mixer's sample rate
    gain: float
    loop: bool


@dataclass
class SectionSpec:
    name: str
    placements: list = field(default_factory=list)  # list[ClipPlacement]


@dataclass
class SongStructureSpec:
    sections: list  # list[SectionSpec]


def _clean_name(name) -> str:
    if not isinstance(name, str):
        raise ValidationError("Each section needs a name.")
    n = _CTRL.sub("", name).strip()
    if not 1 <= len(n) <= MAX_SECTION_NAME:
        raise ValidationError(f"Section names must be 1-{MAX_SECTION_NAME} characters.")
    return n


def validate_structure(raw: dict, known_clip_ids: set) -> dict:
    """Validate and canonicalise a song-structure payload (from the API or the DB). Raises
    ValidationError. Returns the canonical dict, safe to store and to build a SongStructureSpec
    from later (see build_structure_spec in services.py, which loads the actual clip audio)."""
    if not isinstance(raw, dict) or set(raw) - {"sections"}:
        raise ValidationError("Invalid song structure.")
    sections = raw.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
        raise ValidationError(f"A song needs between 1 and {MAX_SECTIONS} sections.")
    seen_ids: set = set()
    out = []
    for sec in sections:
        if not isinstance(sec, dict) or set(sec) - {"id", "name", "clips"}:
            raise ValidationError("Invalid section.")
        name = _clean_name(sec.get("name"))
        sid = sec.get("id")
        if not isinstance(sid, str) or not sid or sid in seen_ids:
            sid = uuid.uuid4().hex[:12]
        seen_ids.add(sid)
        clips_in = sec.get("clips", [])
        if not isinstance(clips_in, list):
            raise ValidationError("Invalid section clip list.")
        clips_out, used = [], set()
        for item in clips_in:
            if not isinstance(item, dict) or set(item) - {"clip_id", "loop"}:
                raise ValidationError("Invalid clip assignment.")
            cid = item.get("clip_id")
            if not isinstance(cid, str) or cid not in known_clip_ids:
                raise ValidationError("A section refers to a clip that doesn't exist in this project.")
            if cid in used:
                continue  # silently de-duplicate rather than error; harmless
            used.add(cid)
            clips_out.append({"clip_id": cid, "loop": bool(item.get("loop", True))})
        out.append({"id": sid, "name": name, "clips": clips_out})
    return {"sections": out}
