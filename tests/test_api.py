import threading
import time
import unittest
from pathlib import Path

from chordforge.errors import StorageError
from tests.helpers import Env, guitar_wav, wait_mixdown

PW = "correct horse battery"


class ApiTestCase(unittest.TestCase):
    mixer = None
    cfg = {}

    def setUp(self):
        self.env = Env(mixer=self.mixer, **self.cfg)

    def tearDown(self):
        self.env.close()

    def new_project(self, c, name=None):
        s, o = c.json("POST", "/api/projects", {"name": name} if name else {})
        self.assertEqual(s, 201, o)
        return o["project"]["id"]

    def upload_clip(self, c, pid, track_type="guitar", label="G1", data=None):
        s, o, _, _ = c.request("PUT", f"/api/projects/{pid}/clips", raw=data if data is not None else guitar_wav(),
                               headers={"X-Track-Type": track_type, "X-Label": label or ""})
        return s, o

    def set_up_placed_clip(self, c, pid):
        clip = self.upload_clip(c, pid)[1]["clip"]
        c.request("PUT", f"/api/projects/{pid}/structure",
                  {"sections": [{"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        return clip

    def stored_files(self):
        return [p for p in Path(self.env.cfg.storage_dir).rglob("*") if p.is_file()]


class TestAuthApi(ApiTestCase):
    def test_register_login_logout_session(self):
        c = self.env.client()
        s, o = c.json("POST", "/api/auth/register", {"email": "A@Example.com", "password": PW})
        self.assertEqual((s, o["user"]["email"]), (201, "a@example.com"))
        self.assertIsNotNone(c.cookie)  # logged in after registering
        s, o = c.json("GET", "/api/auth/me")
        self.assertEqual(s, 200)
        old_cookie = c.cookie
        s, _ = c.json("POST", "/api/auth/logout")
        self.assertEqual(s, 200)
        self.assertEqual(c.json("GET", "/api/auth/me")[0], 401)
        c.cookie = old_cookie  # replaying the old cookie must not work after logout
        self.assertEqual(c.json("GET", "/api/auth/me")[0], 401)
        c.cookie = None
        s, o = c.json("POST", "/api/auth/login", {"email": "a@example.com", "password": PW})
        self.assertEqual(s, 200)
        self.assertEqual(c.json("GET", "/api/auth/me")[0], 200)

    def test_cookie_flags(self):
        c = self.env.client()
        _, _, _, r = c.request("POST", "/api/auth/register", {"email": "a@example.com", "password": PW})
        sc = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)
        self.assertNotIn("Secure", sc)  # only when COOKIE_SECURE=1

    def test_duplicate_account(self):
        self.env.signed_in()
        c2 = self.env.client()
        s, o = c2.json("POST", "/api/auth/register", {"email": "a@example.com", "password": PW})
        self.assertEqual((s, o["error"]["code"]), (409, "email_taken"))
        self.assertIsNone(c2.cookie)

    def test_incorrect_password_and_unknown_user_same_response(self):
        self.env.signed_in()
        c = self.env.client()
        r1 = c.json("POST", "/api/auth/login", {"email": "a@example.com", "password": "wrong password!"})
        r2 = c.json("POST", "/api/auth/login", {"email": "ghost@example.com", "password": PW})
        self.assertEqual(r1, r2)
        self.assertEqual(r1[0], 401)
        self.assertIsNone(c.cookie)

    def test_input_validation(self):
        c = self.env.client()
        for body in ({}, {"email": "bad", "password": PW}, {"email": "a@example.com", "password": "short"},
                     {"email": ["a@example.com"], "password": PW}, {"email": "a@example.com", "password": 12345678901}):
            with self.subTest(body=body):
                self.assertEqual(c.json("POST", "/api/auth/register", body)[0], 400)
        s, o, _, _ = c.request("POST", "/api/auth/login", raw=b"{not json")
        self.assertEqual(s, 400)

    def test_protected_routes_require_session(self):
        c = self.env.client()
        pid = "00000000-0000-4000-8000-000000000000"
        for m, p in (("GET", "/api/projects"), ("POST", "/api/projects"), ("GET", f"/api/projects/{pid}"),
                     ("PATCH", f"/api/projects/{pid}"), ("DELETE", f"/api/projects/{pid}"),
                     ("POST", f"/api/projects/{pid}/mixdown"), ("GET", f"/api/projects/{pid}/mixdown"),
                     ("GET", f"/api/projects/{pid}/clips"), ("GET", f"/api/projects/{pid}/structure")):
            with self.subTest(route=f"{m} {p}"):
                self.assertEqual(c.json(m, p, {} if m in ("POST", "PATCH") else None)[0], 401)
        c.cookie = "cf_session=forged"
        self.assertEqual(c.json("GET", "/api/projects")[0], 401)

    def test_csrf_protection(self):
        c = self.env.signed_in()
        self.assertEqual(c.json("POST", "/api/projects", {}, csrf=False)[0], 403)
        self.assertEqual(c.json("POST", "/api/projects", {}, headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(c.json("POST", "/api/projects", {}, headers={"Origin": f"http://127.0.0.1:{self.env.port}"})[0], 201)

    def test_login_rate_limit_via_http(self):
        self.env.signed_in()
        c = self.env.client()
        codes = [c.json("POST", "/api/auth/login", {"email": "a@example.com", "password": "wrong password!"})[0]
                 for _ in range(12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10:], [429, 429])

    def test_errors_do_not_leak_internals(self):
        c = self.env.signed_in()
        s, o, raw, _ = c.request("GET", "/api/projects/not-a-uuid")
        self.assertEqual(s, 404)
        self.assertNotIn(b"Traceback", raw)
        self.assertNotIn(b"sqlite", raw.lower())
        self.assertNotIn(str(self.env.tmp.name).encode(), raw)


class TestUserIsolationApi(ApiTestCase):
    def test_user_b_cannot_touch_user_a_project(self):
        a = self.env.signed_in("a@example.com")
        b = self.env.signed_in("b@example.com")
        pid = self.new_project(a, "A's song")
        self.set_up_placed_clip(a, pid)
        # A can read their own
        self.assertEqual(a.json("GET", f"/api/projects/{pid}")[0], 200)
        # B gets 404 on everything (indistinguishable from a missing project)
        for m, p, body in (("GET", f"/api/projects/{pid}", None), ("PATCH", f"/api/projects/{pid}", {"name": "hacked"}),
                           ("DELETE", f"/api/projects/{pid}", None), ("GET", f"/api/projects/{pid}/mixdown", None),
                           ("POST", f"/api/projects/{pid}/mixdown", None)):
            with self.subTest(route=f"{m} {p}"):
                self.assertEqual(b.json(m, p, body)[0], 404)
        self.assertEqual(b.json("GET", "/api/projects")[1]["projects"], [])
        # and A's data is intact
        s, o = a.json("GET", f"/api/projects/{pid}")
        self.assertEqual((s, o["project"]["name"]), (200, "A's song"))

    def test_mixdown_audio_isolated(self):
        a = self.env.signed_in("a@example.com")
        b = self.env.signed_in("b@example.com")
        pid = self.new_project(a)
        self.set_up_placed_clip(a, pid)
        a.json("POST", f"/api/projects/{pid}/mixdown")
        wait_mixdown(a, pid)
        self.assertEqual(a.request("GET", f"/api/projects/{pid}/mixdown")[0], 200)
        self.assertEqual(b.request("GET", f"/api/projects/{pid}/mixdown")[0], 404)

    def test_storage_keys_not_exposed(self):
        a = self.env.signed_in()
        pid = self.new_project(a)
        self.set_up_placed_clip(a, pid)
        _, _, raw, _ = a.request("GET", f"/api/projects/{pid}")
        self.assertNotIn(b"clip-", raw)
        self.assertNotIn(b".wav", raw)


class TestProjectsApi(ApiTestCase):
    def test_crud_and_validation(self):
        c = self.env.signed_in()
        pid = self.new_project(c, "  My song  ")
        self.assertEqual(c.json("GET", f"/api/projects/{pid}")[1]["project"]["name"], "My song")
        s, o = c.json("PATCH", f"/api/projects/{pid}", {"name": "Renamed"})
        self.assertEqual(s, 200, o)
        self.assertEqual(o["project"]["name"], "Renamed")
        for bad in ({"name": ""}, {"name": "x" * 500}, {"name": 5}, {"bogus": 1}, {"user_id": "x"},
                    {"generated_key": "../../x"}, {"genre": "pop"}):
            with self.subTest(bad=bad):
                self.assertEqual(c.json("PATCH", f"/api/projects/{pid}", bad)[0], 400)
        self.assertEqual(c.json("GET", "/api/projects")[1]["projects"][0]["name"], "Renamed")
        self.assertEqual(c.json("DELETE", f"/api/projects/{pid}")[0], 200)
        self.assertEqual(c.json("GET", f"/api/projects/{pid}")[0], 404)

    def test_control_characters_stripped_from_name(self):
        c = self.env.signed_in()
        pid = self.new_project(c, "A\x00B\r\nC")
        self.assertEqual(c.json("GET", f"/api/projects/{pid}")[1]["project"]["name"], "ABC")

    def test_project_limit(self):
        self.env.close()
        self.env = Env(max_projects_per_user=2)
        c = self.env.signed_in()
        self.new_project(c); self.new_project(c)
        s, o = c.json("POST", "/api/projects", {})
        self.assertEqual((s, o["error"]["code"]), (409, "project_limit"))

    def test_delete_removes_files(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        self.set_up_placed_clip(c, pid)
        c.json("POST", f"/api/projects/{pid}/mixdown")
        wait_mixdown(c, pid)
        self.assertEqual(len(self.stored_files()), 2)
        c.json("DELETE", f"/api/projects/{pid}")
        self.assertEqual(self.stored_files(), [])

    def test_path_traversal_in_ids(self):
        c = self.env.signed_in()
        for p in ("/api/projects/../auth/me", "/api/projects/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                  "/api/projects/..%2f..%2f/mixdown", "/../../etc/passwd", "/app.js/../../etc/passwd"):
            with self.subTest(p=p):
                s, _, raw, _ = c.request("GET", p)
                self.assertIn(s, (404, 400))
                self.assertNotIn(b"root:", raw)

    def test_static_files_and_headers(self):
        c = self.env.client()
        s, _, raw, r = c.request("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn(b"ChordForge", raw)
        self.assertIn("default-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertNotIn("unsafe-inline", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Frame-Options"), "DENY")
        self.assertNotIn(b"<script>", raw)  # no inline scripts (CSP would block them)
        self.assertEqual(c.request("GET", "/app.js")[0], 200)
        self.assertEqual(c.request("GET", "/styles.css")[0], 200)
        self.assertEqual(c.request("GET", "/server.py")[0], 404)
        s, o = c.json("GET", "/api/config")
        self.assertEqual(s, 200)
        self.assertIn("vocals", [t["id"] for t in o["track_types"]])
        self.assertEqual(c.json("GET", "/api/health")[1], {"status": "ok"})


class GatedMixer:
    """Blocks until released; lets tests observe queued/running states."""

    def __init__(self):
        self.release, self.entered = threading.Event(), threading.Event()

    def __call__(self, structure, seed):
        self.entered.set()
        self.release.wait(10)
        from chordforge.audio.io import encode_wav
        import numpy as np
        return encode_wav(np.zeros(4410, dtype=np.float64), 44100), 0.1


class TestConcurrencyApi(ApiTestCase):
    def setUp(self):
        self.gate = GatedMixer()
        self.mixer = self.gate
        super().setUp()

    def test_busy_states_block_conflicting_actions(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        self.set_up_placed_clip(c, pid)
        self.assertEqual(c.json("POST", f"/api/projects/{pid}/mixdown")[0], 202)
        self.assertTrue(self.gate.entered.wait(10))
        self.assertEqual(c.json("GET", f"/api/projects/{pid}")[1]["project"]["mixdown"]["status"], "running")
        s, o = c.json("POST", f"/api/projects/{pid}/mixdown")
        self.assertEqual((s, o["error"]["code"]), (409, "busy"))
        self.gate.release.set()
        self.assertEqual(wait_mixdown(c, pid)["mixdown"]["status"], "succeeded")

    def test_delete_during_mixdown_leaves_no_orphans(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        self.set_up_placed_clip(c, pid)
        c.json("POST", f"/api/projects/{pid}/mixdown")
        self.assertTrue(self.gate.entered.wait(10))
        self.assertEqual(c.json("DELETE", f"/api/projects/{pid}")[0], 200)
        self.gate.release.set()
        time.sleep(1.0)  # let the job finish and clean up
        self.assertEqual(self.stored_files(), [])


class TestRestartRecovery(ApiTestCase):
    def test_interrupted_jobs_fail_on_restart(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        self.set_up_placed_clip(c, pid)
        uid = c.json("GET", "/api/auth/me")[1]["user"]["id"]
        self.env.app.projects.update(uid, pid, generation_status="running")
        self.assertEqual(self.env.app.projects.fail_interrupted(), 1)
        p = c.json("GET", f"/api/projects/{pid}")[1]["project"]
        self.assertEqual(p["mixdown"]["status"], "failed")
        self.assertIn("restart", p["mixdown"]["error"])


if __name__ == "__main__":
    unittest.main()
