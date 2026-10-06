"""Private file storage behind a small interface. Local-disk implementation with safe keys."""
from __future__ import annotations

import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Protocol

from .errors import NotFound, StorageError

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEY_RE = re.compile(rf"^({_UUID})/({_UUID})/(rec|gen|clip)-({_UUID})\.(wav|mp3|ogg|flac|m4a|webm)$")
EXTS = {"wav", "mp3", "ogg", "flac", "m4a", "webm"}


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def delete_project(self, user_id: str, project_id: str) -> None: ...


def make_key(user_id: str, project_id: str, kind: str, ext: str) -> str:
    """Storage keys are generated server-side from IDs; user-supplied filenames are never used."""
    if kind not in ("rec", "gen", "clip") or ext not in EXTS:
        raise ValueError("invalid key parts")
    key = f"{user_id}/{project_id}/{kind}-{uuid.uuid4()}.{ext}"
    if not KEY_RE.match(key):
        raise ValueError("invalid identifiers for storage key")
    return key


def key_owner(key: str) -> tuple[str, str] | None:
    m = KEY_RE.match(key)
    return (m.group(1), m.group(2)) if m else None


class LocalStorage:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY_RE.match(key):
            raise StorageError("Invalid storage key.")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise StorageError("Invalid storage key.")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        tmp = p.with_name(p.name + ".tmp")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise StorageError("Could not save the file. Please try again.")

    def get(self, key: str) -> bytes:
        p = self._path(key)
        try:
            return p.read_bytes()
        except FileNotFoundError:
            raise NotFound("File not found.")
        except OSError:
            raise StorageError("Could not read the file. Please try again.")

    def delete(self, key: str) -> None:
        p = self._path(key)
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise StorageError("Could not delete the file.")

    def delete_project(self, user_id: str, project_id: str) -> None:
        if not re.fullmatch(_UUID, user_id) or not re.fullmatch(_UUID, project_id):
            raise StorageError("Invalid identifiers.")
        d = (self.root / user_id / project_id).resolve()
        if self.root not in d.parents:
            raise StorageError("Invalid identifiers.")
        try:
            shutil.rmtree(d, ignore_errors=False)
        except FileNotFoundError:
            pass
        except OSError:
            raise StorageError("Could not delete project files.")
