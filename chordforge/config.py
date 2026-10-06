"""Configuration from environment variables. No secrets have defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("./data")
    host: str = "127.0.0.1"
    port: int = 8000
    cookie_secure: bool = False
    trust_proxy: bool = False
    max_upload_bytes: int = 20 * 1024 * 1024
    max_duration_s: float = 60.0
    min_duration_s: float = 2.0
    session_ttl_hours: int = 24 * 7
    max_projects_per_user: int = 50
    # Thread-pool size for background mixdown jobs ("Build my song"). There is no mixdown timeout:
    # unlike a hosted generation provider, this is local NumPy/SciPy processing against the user's
    # own (duration-capped) recordings, which can't hang on a network call, so a deadline mechanism
    # would add complexity without protecting against anything real.
    mixdown_workers: int = 2

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chordforge.sqlite3"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "files"

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        d = cls()
        return cls(
            data_dir=Path(env.get("CHORDFORGE_DATA_DIR", str(d.data_dir))),
            host=env.get("CHORDFORGE_HOST", d.host),
            port=int(env.get("PORT", env.get("CHORDFORGE_PORT", d.port))),
            cookie_secure=_bool(env.get("COOKIE_SECURE"), d.cookie_secure),
            trust_proxy=_bool(env.get("TRUST_PROXY"), d.trust_proxy),
            max_upload_bytes=int(float(env.get("MAX_UPLOAD_MB", 20)) * 1024 * 1024),
            max_duration_s=float(env.get("MAX_DURATION_SECONDS", d.max_duration_s)),
            min_duration_s=float(env.get("MIN_DURATION_SECONDS", d.min_duration_s)),
            session_ttl_hours=int(env.get("SESSION_TTL_HOURS", d.session_ttl_hours)),
            max_projects_per_user=int(env.get("MAX_PROJECTS_PER_USER", d.max_projects_per_user)),
            mixdown_workers=int(env.get("MIXDOWN_WORKERS", d.mixdown_workers)),
        )
