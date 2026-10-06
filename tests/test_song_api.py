"""HTTP API tests for clips, song structure, and the mixdown ("Build my song") workflow."""
import unittest

from tests.helpers import Env, guitar_wav, wait_mixdown


class SongApiTestCase(unittest.TestCase):
    def setUp(self):
        self.env = Env(max_duration_s=120)

    def tearDown(self):
        self.env.close()

    def new_project(self, c, name=None):
        s, o = c.json("POST", "/api/projects", {"name": name} if name else {})
        self.assertEqual(s, 201, o)
        return o["project"]["id"]

    def upload_clip(self, c, pid, track_type="vocals", label="Take 1", data=None):
        s, o, _, _ = c.request("PUT", f"/api/projects/{pid}/clips", raw=data if data is not None else guitar_wav(),
                               headers={"X-Track-Type": track_type, "X-Label": label or ""})
        return s, o


class TestClipsApi(SongApiTestCase):
    def test_upload_list_and_fields(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        s, o = self.upload_clip(c, pid, "vocals", "Chorus vocal")
        self.assertEqual(s, 201, o)
        clip = o["clip"]
        self.assertEqual((clip["track_type"], clip["label"], clip["muted"], clip["gain"]),
                         ("vocals", "Chorus vocal", False, 1.0))
        self.assertGreater(clip["duration"], 5)
        self.assertEqual(clip["trim_start"], 0)
        self.assertAlmostEqual(clip["trim_end"], clip["duration"], places=1)
        s2, o2 = c.json("GET", f"/api/projects/{pid}/clips")
        self.assertEqual(s2, 200)
        self.assertEqual(len(o2["clips"]), 1)
        self.assertEqual(o2["clips"][0]["id"], clip["id"])

    def test_default_label(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        s, o = self.upload_clip(c, pid, "drums", "")
        self.assertEqual(s, 201, o)
        self.assertEqual(o["clip"]["label"], "Drums")

    def test_invalid_track_type_and_bad_audio(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        s, o = self.upload_clip(c, pid, "kazoo", "x")
        self.assertEqual(s, 400, o)
        s2, o2 = self.upload_clip(c, pid, "vocals", "x", data=b"not audio")
        self.assertEqual(s2, 400, o2)
        self.assertEqual(c.json("GET", f"/api/projects/{pid}/clips")[1]["clips"], [])

    def test_update_clip(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        clip = self.upload_clip(c, pid)[1]["clip"]
        s, o = c.json("PATCH", f"/api/projects/{pid}/clips/{clip['id']}",
                      {"trim_start": 1.0, "trim_end": 5.0, "gain": 1.5, "muted": True, "label": "Renamed"})
        self.assertEqual(s, 200, o)
        self.assertEqual((o["clip"]["trim_start"], o["clip"]["trim_end"], o["clip"]["gain"], o["clip"]["muted"],
                          o["clip"]["label"]), (1.0, 5.0, 1.5, True, "Renamed"))
        for bad in ({"gain": 10}, {"trim_start": 5, "trim_end": 1}, {"track_type": "kazoo"}, {"bogus": 1}):
            with self.subTest(bad=bad):
                self.assertEqual(c.json("PATCH", f"/api/projects/{pid}/clips/{clip['id']}", bad)[0], 400)

    def test_delete_clip(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        clip = self.upload_clip(c, pid)[1]["clip"]
        self.assertEqual(c.json("DELETE", f"/api/projects/{pid}/clips/{clip['id']}")[0], 200)
        self.assertEqual(c.json("GET", f"/api/projects/{pid}/clips")[1]["clips"], [])
        self.assertEqual(c.json("DELETE", f"/api/projects/{pid}/clips/{clip['id']}")[0], 404)

    def test_clip_audio_playback_and_range(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        wav = guitar_wav()
        clip = self.upload_clip(c, pid, data=wav)[1]["clip"]
        s, _, raw, r = c.request("GET", f"/api/projects/{pid}/clips/{clip['id']}/audio")
        self.assertEqual((s, raw, r.getheader("Content-Type")), (200, wav, "audio/wav"))
        s2, _, raw2, r2 = c.request("GET", f"/api/projects/{pid}/clips/{clip['id']}/audio",
                                    headers={"Range": "bytes=0-99"})
        self.assertEqual((s2, len(raw2)), (206, 100))

    def test_isolated_per_user(self):
        a = self.env.signed_in("a@example.com")
        b = self.env.signed_in("b@example.com")
        pid = self.new_project(a)
        clip = self.upload_clip(a, pid)[1]["clip"]
        self.assertEqual(b.json("GET", f"/api/projects/{pid}/clips")[0], 404)
        self.assertEqual(b.json("PATCH", f"/api/projects/{pid}/clips/{clip['id']}", {"gain": 2.0})[0], 404)
        self.assertEqual(b.json("DELETE", f"/api/projects/{pid}/clips/{clip['id']}")[0], 404)
        self.assertEqual(b.request("GET", f"/api/projects/{pid}/clips/{clip['id']}/audio")[0], 404)
        s, o = self.upload_clip(b, pid)
        self.assertEqual(s, 404)  # can't even upload a clip to a project you don't own

    def test_requires_auth(self):
        c = self.env.client()
        pid_fake = "00000000-0000-4000-8000-000000000000"
        self.assertEqual(c.json("GET", f"/api/projects/{pid_fake}/clips")[0], 401)
        self.assertEqual(self.upload_clip(c, pid_fake)[0], 401)


class TestStructureApi(SongApiTestCase):
    def test_default_empty(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        s, o = c.json("GET", f"/api/projects/{pid}/structure")
        self.assertEqual((s, o["structure"]), (200, {"sections": []}))

    def test_set_and_get(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        clip = self.upload_clip(c, pid)[1]["clip"]
        structure = {"sections": [{"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]}
        s, o = c.request("PUT", f"/api/projects/{pid}/structure", structure)[:2]
        self.assertEqual(s, 200, o)
        self.assertEqual(o["structure"]["sections"][0]["name"], "Verse")
        s2, o2 = c.json("GET", f"/api/projects/{pid}/structure")
        self.assertEqual(o2["structure"], o["structure"])

    def test_rejects_unknown_clip(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        bad = {"sections": [{"name": "V", "clips": [{"clip_id": "ghost"}]}]}
        s, o = c.request("PUT", f"/api/projects/{pid}/structure", bad)[:2]
        self.assertEqual(s, 400, o)

    def test_isolated_per_user(self):
        a = self.env.signed_in("a@example.com")
        b = self.env.signed_in("b@example.com")
        pid = self.new_project(a)
        self.assertEqual(b.json("GET", f"/api/projects/{pid}/structure")[0], 404)
        s, _, _, _ = b.request("PUT", f"/api/projects/{pid}/structure", {"sections": []})
        self.assertEqual(s, 404)


class TestMixdownApi(SongApiTestCase):
    def test_full_flow_build_play_download(self):
        c = self.env.signed_in()
        pid = self.new_project(c, "Demo song!")
        clip = self.upload_clip(c, pid, "guitar", "G1")[1]["clip"]
        c.request("PUT", f"/api/projects/{pid}/structure",
                  {"sections": [{"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        s, o = c.json("POST", f"/api/projects/{pid}/mixdown")
        self.assertEqual(s, 202, o)
        self.assertIn(o["project"]["mixdown"]["status"], ("queued", "running", "succeeded"))
        p = wait_mixdown(c, pid)
        m = p["mixdown"]
        self.assertEqual(m["status"], "succeeded", m)
        self.assertTrue(m["has_audio"])
        s, _, raw, r = c.request("GET", f"/api/projects/{pid}/mixdown")
        self.assertEqual((s, r.getheader("Content-Type")), (200, "audio/wav"))
        self.assertIsNone(r.getheader("Content-Disposition"))
        self.assertEqual(c.request("GET", f"/api/projects/{pid}/mixdown", headers={"Range": "bytes=0-99"})[0], 206)
        s, _, raw2, r = c.request("GET", f"/api/projects/{pid}/mixdown?download=1")
        self.assertEqual(raw2, raw)
        self.assertEqual(r.getheader("Content-Disposition"), 'attachment; filename="chordforge-demo-song.wav"')

    def test_rejects_with_no_sound_source(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        s, o = c.json("POST", f"/api/projects/{pid}/mixdown")
        self.assertEqual(s, 400, o)

    def test_mixdown_before_build_is_404(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        self.assertEqual(c.request("GET", f"/api/projects/{pid}/mixdown")[0], 404)

    def test_isolated_per_user(self):
        a = self.env.signed_in("a@example.com")
        b = self.env.signed_in("b@example.com")
        pid = self.new_project(a)
        clip = self.upload_clip(a, pid, "guitar", "G1")[1]["clip"]
        a.request("PUT", f"/api/projects/{pid}/structure",
                  {"sections": [{"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]})
        a.json("POST", f"/api/projects/{pid}/mixdown")
        wait_mixdown(a, pid)
        self.assertEqual(b.request("GET", f"/api/projects/{pid}/mixdown")[0], 404)
        self.assertEqual(b.json("POST", f"/api/projects/{pid}/mixdown")[0], 404)

    def test_deleting_clip_removes_it_from_saved_structure(self):
        c = self.env.signed_in()
        pid = self.new_project(c)
        clip = self.upload_clip(c, pid)[1]["clip"]
        structure = {"sections": [{"name": "Verse", "clips": [{"clip_id": clip["id"], "loop": True}]}]}
        c.request("PUT", f"/api/projects/{pid}/structure", structure)
        c.json("DELETE", f"/api/projects/{pid}/clips/{clip['id']}")
        s, o = c.json("GET", f"/api/projects/{pid}/structure")
        self.assertEqual(o["structure"]["sections"][0]["clips"], [])


if __name__ == "__main__":
    unittest.main()
