"""Service-layer tests for clips, song structure, and the mixdown ("Build my song") workflow."""
import time
import unittest

from chordforge.errors import Conflict, NotFound, ValidationError
from tests.helpers import Env, guitar_wav


class TestClipService(unittest.TestCase):
    def setUp(self):
        self.env = Env(max_duration_s=120)
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]

    def tearDown(self):
        self.env.close()

    def test_upload_list_update_delete(self):
        c = self.svc.upload_clip(self.uid, self.pid, "vocals", "Take 1", guitar_wav())
        self.assertEqual((c["track_type"], c["label"], c["muted"]), ("vocals", "Take 1", False))
        self.assertEqual(len(self.svc.list_clips(self.uid, self.pid)), 1)
        upd = self.svc.update_clip(self.uid, self.pid, c["id"], {"gain": 1.8, "muted": True})
        self.assertEqual((upd["gain"], upd["muted"]), (1.8, True))
        self.svc.delete_clip(self.uid, self.pid, c["id"])
        self.assertEqual(self.svc.list_clips(self.uid, self.pid), [])

    def test_default_label_from_track_type(self):
        c = self.svc.upload_clip(self.uid, self.pid, "drums", None, guitar_wav())
        self.assertEqual(c["label"], "Drums")

    def test_invalid_track_type_rejected(self):
        with self.assertRaises(ValidationError):
            self.svc.upload_clip(self.uid, self.pid, "kazoo", "x", guitar_wav())

    def test_invalid_audio_rejected(self):
        with self.assertRaises(Exception):
            self.svc.upload_clip(self.uid, self.pid, "vocals", "x", b"not audio")
        self.assertEqual(self.svc.list_clips(self.uid, self.pid), [])

    def test_silent_audio_rejected(self):
        import numpy as np
        from chordforge.audio.io import encode_wav
        from tests.synth import SR
        silent = encode_wav(np.zeros(SR * 3), SR)
        with self.assertRaises(ValidationError) as cm:
            self.svc.upload_clip(self.uid, self.pid, "vocals", "x", silent)
        self.assertEqual(cm.exception.code, "silent_audio")
        self.assertEqual(self.svc.list_clips(self.uid, self.pid), [])

    def test_trim_validation(self):
        c = self.svc.upload_clip(self.uid, self.pid, "vocals", "x", guitar_wav())
        for bad in ({"trim_start": 5, "trim_end": 1}, {"trim_start": -1, "trim_end": 5},
                    {"trim_start": 0, "trim_end": 9999}, {"trim_start": "a", "trim_end": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.svc.update_clip(self.uid, self.pid, c["id"], bad)

    def test_gain_validation(self):
        c = self.svc.upload_clip(self.uid, self.pid, "vocals", "x", guitar_wav())
        for bad in (0.0, 5.0, -1, "loud", True):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.svc.update_clip(self.uid, self.pid, c["id"], {"gain": bad})
        ok = self.svc.update_clip(self.uid, self.pid, c["id"], {"gain": 2.5})
        self.assertEqual(ok["gain"], 2.5)

    def test_clip_limit(self):
        from chordforge import services as _svc_mod
        old_limit = _svc_mod.MAX_CLIPS_PER_PROJECT
        _svc_mod.MAX_CLIPS_PER_PROJECT = 2
        try:
            self.svc.upload_clip(self.uid, self.pid, "vocals", "1", guitar_wav())
            self.svc.upload_clip(self.uid, self.pid, "vocals", "2", guitar_wav())
            with self.assertRaises(Conflict):
                self.svc.upload_clip(self.uid, self.pid, "vocals", "3", guitar_wav())
        finally:
            _svc_mod.MAX_CLIPS_PER_PROJECT = old_limit

    def test_user_isolation(self):
        c = self.svc.upload_clip(self.uid, self.pid, "vocals", "x", guitar_wav())
        other = self.env.app.auth.register("b@example.com", "correct horse battery")["id"]
        with self.assertRaises(NotFound):
            self.svc.list_clips(other, self.pid)
        with self.assertRaises(NotFound):
            self.svc.update_clip(other, self.pid, c["id"], {"gain": 2.0})
        with self.assertRaises(NotFound):
            self.svc.delete_clip(other, self.pid, c["id"])

    def test_audio_roundtrips(self):
        wav = guitar_wav()
        c = self.svc.upload_clip(self.uid, self.pid, "vocals", "x", wav)
        data, ctype = self.svc.open_clip_audio(self.uid, self.pid, c["id"])
        self.assertEqual((data, ctype), (wav, "audio/wav"))


class TestStructureService(unittest.TestCase):
    def setUp(self):
        self.env = Env(max_duration_s=120)
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]
        self.clip = self.svc.upload_clip(self.uid, self.pid, "vocals", "V1", guitar_wav())

    def tearDown(self):
        self.env.close()

    def test_get_default_empty(self):
        self.assertEqual(self.svc.get_structure(self.uid, self.pid), {"sections": []})

    def test_set_and_get(self):
        raw = {"sections": [{"name": "Verse", "clips": [{"clip_id": self.clip["id"], "loop": True}]}]}
        saved = self.svc.set_structure(self.uid, self.pid, raw)
        self.assertEqual(saved["sections"][0]["name"], "Verse")
        self.assertEqual(self.svc.get_structure(self.uid, self.pid), saved)

    def test_rejects_unknown_clip_id(self):
        with self.assertRaises(ValidationError):
            self.svc.set_structure(self.uid, self.pid, {"sections": [{"name": "V", "clips": [{"clip_id": "ghost"}]}]})

    def test_deleting_clip_cleans_structure(self):
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": self.clip["id"], "loop": True}]}]})
        self.svc.delete_clip(self.uid, self.pid, self.clip["id"])
        got = self.svc.get_structure(self.uid, self.pid)
        self.assertEqual(got["sections"][0]["clips"], [])

    def test_isolated_per_user(self):
        other = self.env.app.auth.register("b@example.com", "correct horse battery")["id"]
        with self.assertRaises(NotFound):
            self.svc.get_structure(other, self.pid)
        with self.assertRaises(NotFound):
            self.svc.set_structure(other, self.pid, {"sections": []})


def fake_mixer(structure, seed):
    """A fast stand-in for chordforge.mixing.mix_structure -- lets JobRunner tests run without
    doing real DSP or waiting on real timing (mirrors the old GateProvider/Flaky test doubles)."""
    from chordforge.audio.io import encode_wav
    import numpy as np
    return encode_wav(np.zeros(4410, dtype=np.float64), 44100), 0.1


class FailingMixer:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, structure, seed):
        raise self.exc


class GatedMixer:
    """Blocks until released; lets tests observe queued/running states."""

    def __init__(self):
        import threading
        self.release, self.entered = threading.Event(), threading.Event()

    def __call__(self, structure, seed):
        self.entered.set()
        self.release.wait(10)
        return fake_mixer(structure, seed)


class TestMixdownService(unittest.TestCase):
    def setUp(self):
        self.env = Env(mixer=fake_mixer)
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]

    def tearDown(self):
        self.env.close()

    def wait(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            p = self.svc.get(self.uid, self.pid)
            if p["mixdown"]["status"] in ("succeeded", "failed"):
                return p
            time.sleep(0.05)
        raise AssertionError("timed out")

    def test_rejects_mixdown_with_no_structure(self):
        with self.assertRaises(ValidationError):
            self.svc.start_mixdown(self.uid, self.pid)

    def test_rejects_mixdown_with_empty_sections(self):
        self.svc.set_structure(self.uid, self.pid, {"sections": [{"name": "Intro", "clips": []}]})
        with self.assertRaises(ValidationError):
            self.svc.start_mixdown(self.uid, self.pid)

    def test_succeeds_with_a_placed_clip(self):
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        p = self.svc.start_mixdown(self.uid, self.pid)
        self.assertEqual(p["mixdown"]["status"], "queued")
        p = self.wait()
        self.assertEqual(p["mixdown"]["status"], "succeeded")
        self.assertTrue(p["mixdown"]["has_audio"])
        data, ctype, proj = self.svc.open_mixdown_audio(self.uid, self.pid)
        self.assertEqual(ctype, "audio/wav")

    def test_muted_clip_is_excluded(self):
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.update_clip(self.uid, self.pid, clip["id"], {"muted": True})
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        with self.assertRaises(ValidationError):
            self.svc.start_mixdown(self.uid, self.pid)

    def test_busy_state_blocks_another_mixdown(self):
        gate = GatedMixer()
        self.env.close()
        self.env = Env(mixer=gate)
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        self.svc.start_mixdown(self.uid, self.pid)
        self.assertTrue(gate.entered.wait(10))
        with self.assertRaises(Conflict):
            self.svc.start_mixdown(self.uid, self.pid)
        gate.release.set()

    def test_failure_is_reported_never_faked(self):
        self.env.close()
        self.env = Env(mixer=FailingMixer(RuntimeError("secret /var/db detail")))
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        self.svc.start_mixdown(self.uid, self.pid)
        p = self.wait()
        self.assertEqual(p["mixdown"]["status"], "failed")
        self.assertNotIn("secret", p["mixdown"]["error"])
        self.assertNotIn("/var", p["mixdown"]["error"])
        self.assertFalse(p["mixdown"]["has_audio"])

    def test_empty_mixdown_error_is_reported_cleanly(self):
        from chordforge.mixing import MixdownEmptyError
        self.env.close()
        self.env = Env(mixer=FailingMixer(MixdownEmptyError("Your song is empty.")))
        self.svc = self.env.app.svc
        u = self.env.app.auth.register("a@example.com", "correct horse battery")
        self.uid = u["id"]
        self.pid = self.svc.create(self.uid, "Song")["id"]
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        self.svc.start_mixdown(self.uid, self.pid)
        p = self.wait()
        self.assertEqual(p["mixdown"]["status"], "failed")
        self.assertIn("empty", p["mixdown"]["error"])

    def test_user_isolation(self):
        other = self.env.app.auth.register("b@example.com", "correct horse battery")["id"]
        with self.assertRaises(NotFound):
            self.svc.start_mixdown(other, self.pid)
        with self.assertRaises(NotFound):
            self.svc.open_mixdown_audio(other, self.pid)

    def test_open_mixdown_audio_before_any_run_is_not_found(self):
        with self.assertRaises(NotFound):
            self.svc.open_mixdown_audio(self.uid, self.pid)

    def test_old_file_is_replaced_not_leaked(self):
        clip = self.svc.upload_clip(self.uid, self.pid, "guitar", "G1", guitar_wav())
        self.svc.set_structure(self.uid, self.pid, {"sections": [
            {"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        self.svc.start_mixdown(self.uid, self.pid)
        self.wait()
        self.svc.start_mixdown(self.uid, self.pid)
        p = self.wait()
        self.assertEqual(p["mixdown"]["status"], "succeeded")
        from pathlib import Path
        files = [f for f in Path(self.env.cfg.storage_dir).rglob("*") if f.is_file()]
        gen_files = [f for f in files if "gen-" in f.name or f.suffix == ".wav"]
        # exactly one mixdown wav should remain for this project (plus the clip's own wav)
        self.assertEqual(len(files), 2)


if __name__ == "__main__":
    unittest.main()
