"""SQLite database with simple versioned migrations and an ownership-enforcing repository."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import Conflict, NotFound

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX idx_sessions_user ON sessions(user_id);
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        recording_key TEXT,
        recording_bytes INTEGER,
        recording_duration REAL,
        analysis_json TEXT,
        bpm REAL,
        musical_key TEXT,
        progression_json TEXT,
        genre TEXT,
        mood TEXT,
        settings_json TEXT,
        generated_key TEXT,
        generated_duration REAL,
        generation_status TEXT NOT NULL DEFAULT 'none',
        generation_error TEXT,
        generation_attempts INTEGER NOT NULL DEFAULT 0,
        generation_updated_at TEXT
    );
    CREATE INDEX idx_projects_user ON projects(user_id, updated_at);
    """,
    # 2: multi-stem clips + song structure (see chordforge/song.py for the rendering side)
    """
    ALTER TABLE projects ADD COLUMN song_structure_json TEXT;
    CREATE TABLE clips (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        track_type TEXT NOT NULL,
        label TEXT NOT NULL,
        storage_key TEXT NOT NULL,
        format TEXT NOT NULL,
        duration REAL NOT NULL,
        trim_start REAL NOT NULL DEFAULT 0,
        trim_end REAL NOT NULL,
        gain REAL NOT NULL DEFAULT 1.0,
        muted INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX idx_clips_project ON clips(project_id, position);
    """,
    # 3: originally tagged a clip as a real recording vs. a synthesised instrument take the user
    # placed themselves. That generate-and-place feature was removed along with all synthesis/
    # generation (see PROJECT_STATUS.md) -- every clip is a real recording/upload now, so this
    # column is unused dead weight (always 'recorded'), kept rather than risking a migration to
    # drop it.
    """
    ALTER TABLE clips ADD COLUMN source TEXT NOT NULL DEFAULT 'recorded';
    """,
]

# Columns a caller may set through update_project. Several columns from the old chord-analysis/
# AI-generation design still physically exist in the table (recording_key, analysis_json, bpm,
# musical_key, progression_json, genre, mood, settings_json) but are no longer written or read by
# any code -- left in place rather than risking a DROP COLUMN migration for columns that are
# already just harmless dead weight (always NULL for every project created since this list was
# trimmed). generated_key/generated_duration/generation_status/generation_error/
# generation_attempts are reused for the mixdown (see chordforge/mixing.py), not a fresh column
# set, to avoid an unnecessary migration for what is purely a renaming of what those columns mean.
PROJECT_FIELDS = {
    "name", "song_structure_json", "generated_key", "generated_duration",
    "generation_status", "generation_error", "generation_attempts",
}
STATUSES = ("none", "queued", "running", "succeeded", "failed")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._keepalive = None
        if self.path == ":memory:":  # shared in-memory DB for tests
            self.path = f"file:cf{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._keepalive = self._raw_connect()
        self.migrate()

    def _raw_connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, uri=self.path.startswith("file:"))
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    @contextmanager
    def connect(self):
        c = self._raw_connect()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def migrate(self) -> None:
        with self.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
            done = {r[0] for r in c.execute("SELECT version FROM schema_migrations")}
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i not in done:
                    c.executescript(sql)
                    c.execute("INSERT INTO schema_migrations(version) VALUES (?)", (i,))
        if self.path != ":memory:" and not self.path.startswith("file:"):
            with self.connect() as c:
                c.execute("PRAGMA journal_mode=WAL")

    def close(self):
        if self._keepalive:
            self._keepalive.close()


class UserRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, email: str, password_hash: str) -> dict:
        uid = str(uuid.uuid4())
        try:
            with self.db.connect() as c:
                c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES (?,?,?,?)",
                          (uid, email, password_hash, now()))
        except sqlite3.IntegrityError:
            raise Conflict("An account with this email already exists.", "email_taken")
        return {"id": uid, "email": email}

    def by_email(self, email: str) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        return dict(r) if r else None

    def by_id(self, uid: str) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT id,email,created_at FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


class SessionRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, token_hash: str, user_id: str, expires_at: str) -> None:
        with self.db.connect() as c:
            c.execute("INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                      (token_hash, user_id, now(), expires_at))

    def lookup(self, token_hash: str) -> dict | None:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(r) if r else None

    def delete(self, token_hash: str) -> None:
        with self.db.connect() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def purge_expired(self) -> None:
        with self.db.connect() as c:
            c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))


class ProjectRepo:
    """Every method that touches a project takes the acting user_id and enforces ownership.
    A project owned by someone else is indistinguishable from a missing one (NotFound)."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: str, name: str) -> dict:
        pid, ts = str(uuid.uuid4()), now()
        with self.db.connect() as c:
            c.execute("INSERT INTO projects(id,user_id,name,created_at,updated_at) VALUES (?,?,?,?,?)",
                      (pid, user_id, name, ts, ts))
        return self.get(user_id, pid)

    def count(self, user_id: str) -> int:
        with self.db.connect() as c:
            return c.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (user_id,)).fetchone()[0]

    def get(self, user_id: str, project_id: str) -> dict:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user_id)).fetchone()
        if not r:
            raise NotFound("Project not found.")
        return dict(r)

    def list(self, user_id: str) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM projects WHERE user_id=? ORDER BY updated_at DESC, created_at DESC",
                             (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def update(self, user_id: str, project_id: str, **fields) -> dict:
        bad = set(fields) - PROJECT_FIELDS
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if "generation_status" in fields and fields["generation_status"] not in STATUSES:
            raise ValueError("invalid generation_status")
        cols = list(fields) + ["updated_at"]
        vals = list(fields.values()) + [now()]
        if "generation_status" in fields:
            cols.append("generation_updated_at")
            vals.append(now())
        sets = ", ".join(f"{c}=?" for c in cols)
        with self.db.connect() as c:
            cur = c.execute(f"UPDATE projects SET {sets} WHERE id=? AND user_id=?", (*vals, project_id, user_id))
            if cur.rowcount == 0:
                raise NotFound("Project not found.")
        return self.get(user_id, project_id)

    def delete(self, user_id: str, project_id: str) -> dict:
        proj = self.get(user_id, project_id)
        with self.db.connect() as c:
            c.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, user_id))
        return proj

    def fail_interrupted(self) -> int:
        """On startup: jobs left queued/running by a previous process can never finish."""
        with self.db.connect() as c:
            cur = c.execute(
                "UPDATE projects SET generation_status='failed', generation_error=? "
                "WHERE generation_status IN ('queued','running')",
                ("Generation was interrupted by a server restart. Please try again.",))
            return cur.rowcount


CLIP_FIELDS = {"track_type", "label", "trim_start", "trim_end", "gain", "muted", "position"}


class ClipRepo:
    """Clips belong to a project, so ownership is enforced by requiring the caller to have
    already loaded that project via ProjectRepo (see ClipService in services.py) -- every method
    here still takes user_id and re-checks it, so a clip can never be read/written across users
    even if a caller forgot the project-ownership check."""

    def __init__(self, db: Database):
        self.db = db

    def create(self, user_id: str, project_id: str, track_type: str, label: str, storage_key: str,
              fmt: str, duration: float, source: str = "recorded") -> dict:
        cid, ts = str(uuid.uuid4()), now()
        with self.db.connect() as c:
            pos = c.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM clips WHERE project_id=?",
                            (project_id,)).fetchone()[0]
            c.execute(
                "INSERT INTO clips(id,project_id,user_id,track_type,label,storage_key,format,duration,"
                "trim_start,trim_end,gain,muted,position,created_at,updated_at,source) "
                "VALUES (?,?,?,?,?,?,?,?,0,?,1.0,0,?,?,?,?)",
                (cid, project_id, user_id, track_type, label, storage_key, fmt, duration, duration, pos, ts, ts,
                 source))
        return self.get(user_id, project_id, cid)

    def get(self, user_id: str, project_id: str, clip_id: str) -> dict:
        with self.db.connect() as c:
            r = c.execute("SELECT * FROM clips WHERE id=? AND project_id=? AND user_id=?",
                          (clip_id, project_id, user_id)).fetchone()
        if not r:
            raise NotFound("Clip not found.")
        return dict(r)

    def list(self, user_id: str, project_id: str) -> list[dict]:
        with self.db.connect() as c:
            rows = c.execute("SELECT * FROM clips WHERE project_id=? AND user_id=? ORDER BY position, created_at",
                             (project_id, user_id)).fetchall()
        return [dict(r) for r in rows]

    def update(self, user_id: str, project_id: str, clip_id: str, **fields) -> dict:
        bad = set(fields) - CLIP_FIELDS
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        cols = list(fields) + ["updated_at"]
        vals = list(fields.values()) + [now()]
        sets = ", ".join(f"{c}=?" for c in cols)
        with self.db.connect() as c:
            cur = c.execute(f"UPDATE clips SET {sets} WHERE id=? AND project_id=? AND user_id=?",
                            (*vals, clip_id, project_id, user_id))
            if cur.rowcount == 0:
                raise NotFound("Clip not found.")
        return self.get(user_id, project_id, clip_id)

    def delete(self, user_id: str, project_id: str, clip_id: str) -> dict:
        clip = self.get(user_id, project_id, clip_id)
        with self.db.connect() as c:
            c.execute("DELETE FROM clips WHERE id=? AND project_id=? AND user_id=?",
                     (clip_id, project_id, user_id))
        return clip


def loads(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default
