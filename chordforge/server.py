"""HTTP server (stdlib) exposing the JSON API and the static web UI."""
from __future__ import annotations

import json
import logging
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .song import TRACK_LABELS, TRACK_TYPES
from .audio.io import ALLOWED_FORMATS, ffmpeg_available
from .auth import AuthService
from .config import Config
from .errors import AppError, AuthError, NotFound, PayloadTooLarge, ValidationError
from .services import build_services

log = logging.getLogger("chordforge")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SESSION_COOKIE = "cf_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "chordforge"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")


class App:
    def __init__(self, cfg: Config, mixer=None):
        self.cfg = cfg
        self.db, self.users, self.sessions, self.projects, self.storage, self.svc = build_services(
            cfg, mixer)
        self.auth = AuthService(cfg, self.users, self.sessions)

    def public_config(self) -> dict:
        return {
            "version": __version__,
            "limits": {"max_upload_mb": self.cfg.max_upload_bytes // (1024 * 1024),
                       "max_duration_s": self.cfg.max_duration_s, "min_duration_s": self.cfg.min_duration_s},
            "formats": list(ALLOWED_FORMATS) if ffmpeg_available() else ["wav"],
            "track_types": [{"id": k, "label": TRACK_LABELS[k]} for k in TRACK_TYPES],
        }


def make_handler(app: App):
    cfg = app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChordForge"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no request lines with query strings/tokens
            log.info("%s %s", self.command, urlsplit(self.path).path)

        # ---------- plumbing ----------
        def client_ip(self) -> str:
            if cfg.trust_proxy:
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[-1].strip()[:64]
            return self.client_address[0]

        def _headers(self, status, ctype, length, extra=None, api=True):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
            self.send_header("Cache-Control", "no-store" if api else "no-cache")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def send_json(self, status, obj, extra=None):
            body = json.dumps(obj).encode()
            self._headers(status, "application/json", len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_error_json(self, err: AppError, extra=None):
            self.send_json(err.status, {"error": {"code": err.code, "message": err.message}}, extra)

        def cookie_token(self):
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                c = SimpleCookie()
                c.load(raw)
                m = c.get(SESSION_COOKIE)
                return m.value if m else None
            except Exception:
                return None

        def session_cookie(self, token: str | None) -> dict:
            base = f"{SESSION_COOKIE}={token or ''}; Path=/; HttpOnly; SameSite=Lax"
            if cfg.cookie_secure:
                base += "; Secure"
            base += f"; Max-Age={cfg.session_ttl_hours * 3600 if token else 0}"
            return {"Set-Cookie": base}

        def current_user(self):
            user = app.auth.user_for_token(self.cookie_token())
            if not user:
                raise AuthError("Please sign in.")
            return user

        def check_csrf(self):
            if self.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise _forbidden()
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host", ""):
                raise _forbidden()

        def read_body(self, limit: int) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise ValidationError("Chunked uploads are not supported.")
            cl = self.headers.get("Content-Length")
            if cl is None:
                raise _length_required()
            try:
                n = int(cl)
            except ValueError:
                raise ValidationError("Invalid Content-Length.")
            if n < 0:
                raise ValidationError("Invalid Content-Length.")
            if n > limit:
                # do not read the body; close the connection after responding
                self.close_connection = True
                raise PayloadTooLarge(f"Request is too large (limit {limit // (1024 * 1024) or 1} MB).")
            data = self.rfile.read(n)
            if len(data) != n:
                raise ValidationError("Incomplete request body.")
            return data

        def read_json(self) -> dict:
            raw = self.read_body(64 * 1024)
            if not raw:
                return {}
            try:
                obj = json.loads(raw)
            except ValueError:
                raise ValidationError("Request body must be valid JSON.")
            if not isinstance(obj, dict):
                raise ValidationError("Request body must be a JSON object.")
            return obj

        # ---------- dispatch ----------
        def do_GET(self):
            self._dispatch()

        do_HEAD = do_GET

        def do_POST(self):
            self._dispatch()

        def do_PUT(self):
            self._dispatch()

        def do_PATCH(self):
            self._dispatch()

        def do_DELETE(self):
            self._dispatch()

        def _dispatch(self):
            try:
                path = urlsplit(self.path).path
                method = self.command
                if method in ("GET", "HEAD") and path in STATIC:
                    return self.serve_static(path)
                if not path.startswith("/api/"):
                    raise NotFound("Not found.")
                if method not in ("GET", "HEAD"):
                    self.check_csrf()
                self.route(method, path)
            except AppError as e:
                self.send_error_json(e)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                log.exception("unhandled error on %s %s", self.command, urlsplit(self.path).path)
                try:
                    self.send_json(500, {"error": {"code": "internal_error",
                                                   "message": "Something went wrong. Please try again."}})
                except Exception:
                    self.close_connection = True

        def serve_static(self, path):
            name, ctype = STATIC[path]
            f = WEB_DIR / name
            try:
                body = f.read_bytes()
            except OSError:
                raise NotFound("Not found.")
            self._headers(200, ctype, len(body), api=False)
            if self.command != "HEAD":
                self.wfile.write(body)

        def route(self, method, path):
            if path == "/api/health" and method in ("GET", "HEAD"):
                return self.send_json(200, {"status": "ok"})
            if path == "/api/config" and method == "GET":
                return self.send_json(200, app.public_config())

            # ---- auth ----
            if path == "/api/auth/register" and method == "POST":
                body = self.read_json()
                user = app.auth.register(body.get("email"), body.get("password"), self.client_ip())
                _, token = app.auth.login(body["email"], body["password"], self.client_ip())
                return self.send_json(201, {"user": user}, self.session_cookie(token))
            if path == "/api/auth/login" and method == "POST":
                body = self.read_json()
                user, token = app.auth.login(body.get("email"), body.get("password"), self.client_ip())
                return self.send_json(200, {"user": user}, self.session_cookie(token))
            if path == "/api/auth/logout" and method == "POST":
                app.auth.logout(self.cookie_token())
                return self.send_json(200, {"ok": True}, self.session_cookie(None))
            if path == "/api/auth/me" and method == "GET":
                user = self.current_user()
                return self.send_json(200, {"user": {"id": user["id"], "email": user["email"]}})

            # ---- projects (all require a session) ----
            if path == "/api/projects":
                user = self.current_user()
                if method == "GET":
                    return self.send_json(200, {"projects": app.svc.list(user["id"])})
                if method == "POST":
                    body = self.read_json()
                    return self.send_json(201, {"project": app.svc.create(user["id"], body.get("name"))})
            m = re.fullmatch(rf"/api/projects/({UUID})", path)
            if m:
                user = self.current_user()
                pid = m.group(1)
                if method == "GET":
                    return self.send_json(200, {"project": app.svc.get(user["id"], pid)})
                if method == "PATCH":
                    return self.send_json(200, {"project": app.svc.update(user["id"], pid, self.read_json())})
                if method == "DELETE":
                    app.svc.delete(user["id"], pid)
                    return self.send_json(200, {"ok": True})
            m = re.fullmatch(rf"/api/projects/({UUID})/mixdown", path)
            if m:
                user, pid = self.current_user(), m.group(1)
                if method == "POST":
                    proj = app.svc.start_mixdown(user["id"], pid)
                    return self.send_json(202, {"project": proj})
                if method in ("GET", "HEAD"):
                    return self.serve_mixdown_audio(user["id"], pid)

            # ---- clips (multi-stem recordings) ----
            m = re.fullmatch(rf"/api/projects/({UUID})/clips", path)
            if m:
                user, pid = self.current_user(), m.group(1)
                if method == "PUT":
                    from urllib.parse import unquote
                    track_type = self.headers.get("X-Track-Type", "")
                    label = unquote(self.headers.get("X-Label", "")) or None
                    data = self.read_body(cfg.max_upload_bytes)
                    clip = app.svc.upload_clip(user["id"], pid, track_type, label, data)
                    return self.send_json(201, {"clip": clip})
                if method == "GET":
                    return self.send_json(200, {"clips": app.svc.list_clips(user["id"], pid)})
            m = re.fullmatch(rf"/api/projects/({UUID})/clips/({UUID})", path)
            if m:
                user, pid, cid = self.current_user(), m.group(1), m.group(2)
                if method == "PATCH":
                    return self.send_json(200, {"clip": app.svc.update_clip(user["id"], pid, cid, self.read_json())})
                if method == "DELETE":
                    app.svc.delete_clip(user["id"], pid, cid)
                    return self.send_json(200, {"ok": True})
            m = re.fullmatch(rf"/api/projects/({UUID})/clips/({UUID})/audio", path)
            if m and method in ("GET", "HEAD"):
                user = self.current_user()
                return self.serve_clip_audio(user["id"], m.group(1), m.group(2))

            # ---- song structure ----
            m = re.fullmatch(rf"/api/projects/({UUID})/structure", path)
            if m:
                user, pid = self.current_user(), m.group(1)
                if method == "GET":
                    return self.send_json(200, {"structure": app.svc.get_structure(user["id"], pid)})
                if method == "PUT":
                    return self.send_json(200, {"structure": app.svc.set_structure(user["id"], pid, self.read_json())})
            raise NotFound("Not found.")

        def serve_mixdown_audio(self, user_id, pid):
            data, ctype, proj = app.svc.open_mixdown_audio(user_id, pid)
            extra = {}
            if "download=1" in (urlsplit(self.path).query or ""):
                slug = re.sub(r"[^a-z0-9]+", "-", proj["name"].lower()).strip("-")[:40] or "track"
                extra["Content-Disposition"] = f'attachment; filename="chordforge-{slug}.wav"'
            self._serve_range(data, ctype, extra)

        def serve_clip_audio(self, user_id, pid, clip_id):
            data, ctype = app.svc.open_clip_audio(user_id, pid, clip_id)
            self._serve_range(data, ctype, {})

        def _serve_range(self, data, ctype, extra):
            total = len(data)
            extra = {"Accept-Ranges": "bytes", **extra}
            rng = self.headers.get("Range")
            start, end, status = 0, total - 1, 200
            if rng:
                m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
                if not m or (m.group(1) == "" and m.group(2) == ""):
                    return self._headers(416, "application/json", 0, {"Content-Range": f"bytes */{total}"})
                if m.group(1) == "":
                    n = int(m.group(2))
                    start, end = max(0, total - n), total - 1
                else:
                    start = int(m.group(1))
                    end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
                if start >= total or start > end:
                    return self._headers(416, "application/json", 0, {"Content-Range": f"bytes */{total}"})
                status = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{total}"
            body = data[start:end + 1]
            self._headers(status, ctype, len(body), extra)
            if self.command != "HEAD":
                self.wfile.write(body)

    return Handler


def _forbidden():
    e = AppError("Forbidden.", "forbidden")
    e.status = 403
    return e


def _length_required():
    e = AppError("Content-Length required.", "length_required")
    e.status = 411
    return e


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Client disconnects mid-response are routine (nosy port scans, aborted uploads,
        # slow-client tests); do not spam stderr with a traceback for them.
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def create_server(app: App) -> ThreadingHTTPServer:
    srv = Server((app.cfg.host, app.cfg.port), make_handler(app))
    srv.daemon_threads = True
    return srv
