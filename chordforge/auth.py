"""Registration, login, sessions. Passwords are hashed with scrypt; session tokens are stored hashed."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import SessionRepo, UserRepo, now
from .errors import AuthError, TooManyRequests, ValidationError

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,}$")
MIN_PW, MAX_PW = 10, 128


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024)
    return "scrypt${}${}${}${}${}".format(SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(),
                                          base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password("dummy-password-for-timing")


def normalize_email(email) -> str:
    if not isinstance(email, str):
        raise ValidationError("Enter a valid email address.")
    e = email.strip().lower()
    if len(e) > 254 or not EMAIL_RE.match(e) or ".." in e:
        raise ValidationError("Enter a valid email address.")
    return e


def validate_password(pw) -> str:
    if not isinstance(pw, str) or not MIN_PW <= len(pw) <= MAX_PW:
        raise ValidationError(f"Password must be {MIN_PW}-{MAX_PW} characters.")
    return pw


class RateLimiter:
    """Sliding-window limiter (in-memory, per process)."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        t = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and t - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                raise TooManyRequests("Too many attempts. Please wait a few minutes and try again.")
            q.append(t)

    def reset(self, key: str) -> None:
        with self.lock:
            self.hits.pop(key, None)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, cfg: Config, users: UserRepo, sessions: SessionRepo):
        self.cfg, self.users, self.sessions = cfg, users, sessions
        self.login_limiter = RateLimiter(10, 15 * 60)
        self.register_limiter = RateLimiter(10, 60 * 60)

    def register(self, email, password, client_ip: str = "-") -> dict:
        self.register_limiter.check(client_ip)
        e, pw = normalize_email(email), validate_password(password)
        return self.users.create(e, hash_password(pw))

    def login(self, email, password, client_ip: str = "-") -> tuple[dict, str]:
        if not isinstance(email, str) or not isinstance(password, str) or len(password) > 1024:
            raise AuthError("Invalid email or password.", "invalid_credentials")
        key = f"{client_ip}|{email.strip().lower()}"
        self.login_limiter.check(key)
        user = self.users.by_email(email.strip().lower())
        ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
        if not user or not ok:
            raise AuthError("Invalid email or password.", "invalid_credentials")
        self.login_limiter.reset(key)
        token = secrets.token_urlsafe(32)
        exp = (datetime.now(timezone.utc) + timedelta(hours=self.cfg.session_ttl_hours)).isoformat(timespec="seconds")
        self.sessions.create(_hash_token(token), user["id"], exp)
        return {"id": user["id"], "email": user["email"]}, token

    def user_for_token(self, token: str | None) -> dict | None:
        if not token or len(token) > 200:
            return None
        s = self.sessions.lookup(_hash_token(token))
        if not s or s["expires_at"] < now():
            return None
        return self.users.by_id(s["user_id"])

    def logout(self, token: str | None) -> None:
        if token:
            self.sessions.delete(_hash_token(token))
