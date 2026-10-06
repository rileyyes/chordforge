import tempfile
import unittest
import uuid
from pathlib import Path

from chordforge.auth import (AuthService, RateLimiter, hash_password, normalize_email, validate_password,
                             verify_password)
from chordforge.config import Config
from chordforge.db import MIGRATIONS, Database, ProjectRepo, SessionRepo, UserRepo
from chordforge.errors import (AuthError, Conflict, NotFound, StorageError, TooManyRequests, ValidationError)
from chordforge.storage import LocalStorage, key_owner, make_key


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = LocalStorage(self.tmp.name)
        self.u, self.p = str(uuid.uuid4()), str(uuid.uuid4())

    def tearDown(self):
        self.tmp.cleanup()

    def test_safe_key_generation(self):
        k = make_key(self.u, self.p, "rec", "wav")
        self.assertEqual(key_owner(k), (self.u, self.p))
        self.assertNotEqual(k, make_key(self.u, self.p, "rec", "wav"))
        for bad in (("../x", self.p, "rec", "wav"), (self.u, "a/b", "rec", "wav"), (self.u, self.p, "rec", "exe"),
                    (self.u, self.p, "evil", "wav")):
            with self.assertRaises(ValueError):
                make_key(*bad)

    def test_roundtrip_and_delete(self):
        k = make_key(self.u, self.p, "rec", "wav")
        self.st.put(k, b"abc")
        self.assertEqual(self.st.get(k), b"abc")
        self.st.delete(k)
        with self.assertRaises(NotFound):
            self.st.get(k)
        self.st.delete(k)  # idempotent

    def test_path_traversal_and_bad_keys_rejected(self):
        for bad in ("../../etc/passwd", "/etc/passwd", f"{self.u}/{self.p}/../../x.wav", "a/b/c.wav",
                    f"{self.u}/{self.p}/rec-{uuid.uuid4()}.wav/../../..", "", None, f"{self.u}/{self.p}/rec-x.wav"):
            with self.subTest(bad=bad):
                with self.assertRaises(StorageError):
                    self.st.get(bad)
                with self.assertRaises(StorageError):
                    self.st.put(bad, b"x")
                with self.assertRaises(StorageError):
                    self.st.delete(bad)

    def test_symlink_escape_blocked(self):
        outside = Path(tempfile.mkdtemp())
        (outside / "secret.wav").write_bytes(b"secret")
        (Path(self.tmp.name) / self.u).mkdir()
        (Path(self.tmp.name) / self.u / self.p).symlink_to(outside)
        k = f"{self.u}/{self.p}/rec-{uuid.uuid4()}.wav"
        with self.assertRaises(StorageError):
            self.st.put(k, b"x")

    def test_delete_project_removes_all_files(self):
        for kind in ("rec", "gen"):
            self.st.put(make_key(self.u, self.p, kind, "wav"), b"1")
        self.st.delete_project(self.u, self.p)
        self.assertFalse((Path(self.tmp.name) / self.u / self.p).exists())
        self.st.delete_project(self.u, self.p)  # missing is fine
        with self.assertRaises(StorageError):
            self.st.delete_project("..", self.p)

    def test_write_failure_is_reported_and_leaves_no_partial_file(self):
        k = make_key(self.u, self.p, "rec", "wav")
        d = Path(self.tmp.name) / self.u / self.p
        d.mkdir(parents=True)
        d.chmod(0o500)
        try:
            import os
            if os.geteuid() == 0:
                self.skipTest("root ignores directory permissions")
            with self.assertRaises(StorageError):
                self.st.put(k, b"x")
        finally:
            d.chmod(0o700)
        self.assertEqual([p.name for p in d.iterdir()], [])


class TestDatabaseIsolation(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.users, self.projects = UserRepo(self.db), ProjectRepo(self.db)
        self.a = self.users.create("a@x.com", "h")["id"]
        self.b = self.users.create("b@x.com", "h")["id"]

    def tearDown(self):
        self.db.close()

    def test_user_a_can_access_own_project(self):
        p = self.projects.create(self.a, "Song")
        self.assertEqual(self.projects.get(self.a, p["id"])["name"], "Song")
        self.assertEqual(self.projects.update(self.a, p["id"], name="Renamed")["name"], "Renamed")
        self.assertEqual(len(self.projects.list(self.a)), 1)

    def test_user_a_cannot_access_user_b_project(self):
        p = self.projects.create(self.b, "Secret")
        with self.assertRaises(NotFound):
            self.projects.get(self.a, p["id"])
        with self.assertRaises(NotFound):
            self.projects.update(self.a, p["id"], name="hacked")
        with self.assertRaises(NotFound):
            self.projects.delete(self.a, p["id"])
        self.assertEqual(self.projects.list(self.a), [])
        self.assertEqual(self.projects.get(self.b, p["id"])["name"], "Secret")  # untouched

    def test_update_whitelist_and_status_validation(self):
        p = self.projects.create(self.a, "S")
        with self.assertRaises(ValueError):
            self.projects.update(self.a, p["id"], id="other")
        with self.assertRaises(ValueError):
            self.projects.update(self.a, p["id"], generation_status="hacked")

    def test_sql_injection_is_inert(self):
        p = self.projects.create(self.a, "x'); DROP TABLE projects;--")
        self.assertEqual(self.projects.get(self.a, p["id"])["name"], "x'); DROP TABLE projects;--")
        self.assertEqual(self.users.by_email("' OR '1'='1"), None)

    def test_duplicate_email_and_cascade_delete(self):
        with self.assertRaises(Conflict):
            self.users.create("a@x.com", "h")
        self.projects.create(self.a, "S")
        with self.db.connect() as c:
            c.execute("DELETE FROM users WHERE id=?", (self.a,))
        self.assertEqual(self.projects.count(self.a), 0)

    def test_fail_interrupted_jobs(self):
        p = self.projects.create(self.a, "S")
        self.projects.update(self.a, p["id"], generation_status="running")
        self.assertEqual(self.projects.fail_interrupted(), 1)
        self.assertEqual(self.projects.get(self.a, p["id"])["generation_status"], "failed")

    def test_migrations_are_idempotent(self):
        self.db.migrate()
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], len(MIGRATIONS))


class TestAuthService(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.users, self.sessions = UserRepo(self.db), SessionRepo(self.db)
        self.auth = AuthService(Config(), self.users, self.sessions)

    def tearDown(self):
        self.db.close()

    def test_password_hashing(self):
        h = hash_password("correct horse battery")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertNotIn("correct horse", h)
        self.assertTrue(verify_password("correct horse battery", h))
        self.assertFalse(verify_password("wrong", h))
        self.assertNotEqual(h, hash_password("correct horse battery"))  # per-user salt
        self.assertFalse(verify_password("x", "garbage"))
        self.assertFalse(verify_password("x", "md5$1$2$3$4$5"))

    def test_no_plaintext_password_stored(self):
        self.auth.register("a@x.com", "correct horse battery")
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM users").fetchone()
        self.assertNotIn("correct horse battery", " ".join(str(v) for v in dict(row).values()))

    def test_register_validation(self):
        for e in ("", "nope", "a@b", None, "a b@c.com", "x" * 300 + "@c.com", "a..b@c.com"):
            with self.subTest(email=e), self.assertRaises(ValidationError):
                normalize_email(e)
        for pw in ("short", "", None, "x" * 200, 12345678901):
            with self.subTest(pw=pw), self.assertRaises(ValidationError):
                validate_password(pw)
        self.assertEqual(normalize_email("  A@Example.COM "), "a@example.com")

    def test_duplicate_registration_case_insensitive(self):
        self.auth.register("a@x.com", "correct horse battery")
        with self.assertRaises(Conflict):
            self.auth.register("A@X.com", "another password 1")

    def test_login_logout_session(self):
        self.auth.register("a@x.com", "correct horse battery")
        user, token = self.auth.login("A@x.com", "correct horse battery")
        self.assertEqual(self.auth.user_for_token(token)["email"], "a@x.com")
        with self.db.connect() as c:  # token is stored hashed, never raw
            self.assertEqual(c.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (token,)).fetchone()[0], 0)
        self.auth.logout(token)
        self.assertIsNone(self.auth.user_for_token(token))

    def test_wrong_password_and_unknown_user_look_the_same(self):
        self.auth.register("a@x.com", "correct horse battery")
        msgs = set()
        for e, p in (("a@x.com", "wrong password!"), ("nobody@x.com", "correct horse battery")):
            with self.assertRaises(AuthError) as cm:
                self.auth.login(e, p)
            msgs.add((cm.exception.message, cm.exception.code))
        self.assertEqual(len(msgs), 1)

    def test_bad_tokens(self):
        for t in (None, "", "x" * 500, "not-a-real-token"):
            self.assertIsNone(self.auth.user_for_token(t))

    def test_expired_session(self):
        self.auth.register("a@x.com", "correct horse battery")
        _, token = self.auth.login("a@x.com", "correct horse battery")
        with self.db.connect() as c:
            c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertIsNone(self.auth.user_for_token(token))

    def test_login_rate_limit(self):
        self.auth.register("a@x.com", "correct horse battery")
        for _ in range(10):
            with self.assertRaises(AuthError):
                self.auth.login("a@x.com", "bad password!!", "1.2.3.4")
        with self.assertRaises(TooManyRequests):
            self.auth.login("a@x.com", "correct horse battery", "1.2.3.4")
        self.auth.login("a@x.com", "correct horse battery", "5.6.7.8")  # different client unaffected

    def test_rate_limiter_window(self):
        r = RateLimiter(2, 0.2)
        r.check("k"); r.check("k")
        with self.assertRaises(TooManyRequests):
            r.check("k")
        import time; time.sleep(0.25)
        r.check("k")


if __name__ == "__main__":
    unittest.main()
