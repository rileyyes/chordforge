import http.client
import json
import tempfile
import threading
import time
from pathlib import Path

from chordforge.audio.io import encode_wav
from chordforge.config import Config
from chordforge.server import App, create_server
from tests.synth import SR, progression_audio

_CACHE = {}


def guitar_wav(chords=("Am", "F", "C", "G"), bpm=92, bpc=4, spb=2) -> bytes:
    """A synthesised, guitar-like WAV -- good enough to stand in for a real recording in tests
    that just need *some* non-silent audio (clip upload/trim/mixdown tests). No chord/tempo/key
    detection exists in the app any more, so the chord names here are only there to vary the
    audio's pitch content between test fixtures, not to be recognised."""
    k = (chords, bpm, bpc, spb)
    if k not in _CACHE:
        _CACHE[k] = encode_wav(progression_audio(list(chords), bpm, bpc, spb), SR)
    return _CACHE[k]


class Client:
    def __init__(self, port):
        self.port, self.cookie = port, None

    def request(self, method, path, body=None, raw=None, headers=None, csrf=True):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if csrf:
            h["X-Requested-With"] = "chordforge"
        if self.cookie:
            h["Cookie"] = self.cookie
        data = None
        if raw is not None:
            data, h["Content-Type"] = raw, "application/octet-stream"
        elif body is not None:
            data, h["Content-Type"] = json.dumps(body).encode(), "application/json"
        h.update(headers or {})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.request(method, path, body=data, headers=h)
            r = c.getresponse()
            payload = r.read()
            sc = r.getheader("Set-Cookie")
            if sc:
                v = sc.split(";")[0]
                self.cookie = v if not v.endswith("=") else None
            ctype = r.getheader("Content-Type") or ""
            obj = json.loads(payload) if ctype.startswith("application/json") and payload else None
            return r.status, obj, payload, r
        finally:
            c.close()

    def json(self, method, path, body=None, **kw):
        s, o, _, _ = self.request(method, path, body, **kw)
        return s, o


class Env:
    """A running app on an ephemeral port with a temp data dir. `mixer` is injectable -- see
    JobRunner in chordforge/services.py -- so tests can simulate a slow/failing mixdown without
    doing real DSP work or waiting on real timing."""

    def __init__(self, mixer=None, **cfg_kw):
        self.tmp = tempfile.TemporaryDirectory()
        base = dict(data_dir=Path(self.tmp.name), port=0)
        base.update(cfg_kw)
        self.cfg = Config(**base)
        self.app = App(self.cfg, mixer=mixer)
        self.srv = create_server(self.app)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def client(self) -> Client:
        return Client(self.port)

    def signed_in(self, email="a@example.com", pw="correct horse battery") -> Client:
        c = self.client()
        s, o = c.json("POST", "/api/auth/register", {"email": email, "password": pw})
        assert s == 201, (s, o)
        return c

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.app.svc.runner.pool.shutdown(wait=False)
        self.app.db.close()
        self.tmp.cleanup()


def wait_mixdown(client, pid, want=("succeeded", "failed"), timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        s, o = client.json("GET", f"/api/projects/{pid}")
        st = o["project"]["mixdown"]["status"]
        if st in want:
            return o["project"]
        time.sleep(0.1)
    raise AssertionError("timed out waiting for mixdown")
