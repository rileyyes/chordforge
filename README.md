# ChordForge

> Record your guitar and vocals, section by section. Let the mix come together.

ChordForge is a recording and editing tool, not a generator. You record your guitar for each part
of a song (Intro, Verse, Chorus, ...) and layer your vocals over it. When you're ready, ChordForge
assembles your sections in order and runs your own recordings through a real, fixed mixing chain
-- leveling, ducking the guitar under your vocals, light compression/EQ, section crossfades, and a
final limiter -- so the result sounds like a finished, reasonably polished take of *your*
performance. Nothing is synthesised or AI-generated; every sound in the output is something you
actually played or sang.

## What it actually does right now

- Records guitar or vocal takes in the browser (or accepts an upload: WAV always; MP3/OGG/FLAC/M4A/
  WebM if the server has ffmpeg installed), any number of them, each labeled by part (vocals,
  guitar, drums, bass, keys, lead, other).
- Lets you trim, adjust the volume of, and mute each recording independently.
- Lets you play an existing recording back through headphones while recording a new one, so you can
  perform in time with something you already laid down (e.g. sing over your guitar).
- Lets you arrange your recordings into named song sections (Intro/Verse/Chorus/...); a section's
  length is simply the length of the longest part placed in it -- there's nothing to configure.
- Builds your song with "Build my song": assembles the sections in order and mixes every part
  placed in them with a fixed digital-signal-processing chain (see "How the mix works" below),
  which you can play back, save, and download as a WAV file.
- Has accounts, so your projects are private and persist between visits.

## Quick start

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m chordforge
```

Then open http://127.0.0.1:8000. Everything is stored under `./data` by default (see
`CHORDFORGE_DATA_DIR` in `.env.example`). No account, API key, or external service is required to
run it locally.

Run the tests with `python3 -m tests.run_all` from the repo root.

## How the mix works (read this before assuming it's AI)

"Build my song" is the only automated step in the app, and it is ordinary digital signal
processing over your own recordings -- there is no neural network, no generative model, and no
network call involved. For each section, it:

1. Auto-levels every clip placed in it, so a quiet phone-mic take and a hot DI guitar don't need
   manual gain-riding to sit together.
2. Splits the section into a vocal bus and an instrumental bed, and ducks the bed slightly whenever
   the vocal is present, so your voice sits on top instead of fighting the guitar for space.
3. Applies a fixed, gentle EQ and compression to each bus (a high-pass to clear rumble, a small EQ
   move to carve space for the vocal, and light compression so levels don't swing wildly).

Sections are then joined with a short crossfade instead of a hard cut, and the whole mix passes
through a final soft limiter so it never clips. Every stage is a small, independently-tested
function in `chordforge/mixing.py` (see `tests/test_mixing.py`). None of it is tunable by genre or
mood -- it's the same chain regardless of what you recorded, which is why there's no style picker
in the UI for this.

## Architecture

Single Python process: stdlib `http.server` for the API, SQLite for the database, local disk for
file storage, NumPy/SciPy for the mixing DSP, and a small hand-written HTML/CSS/JS frontend with no
build step. See `docs/ARCHITECTURE.md` for the original, more elaborate plan (Next.js/FastAPI/
Supabase) and `PROJECT_STATUS.md` for why it changed, what was built and tested, and how the app's
scope changed over time (including an earlier chord-detection-and-generation version that has since
been entirely removed -- see PROJECT_STATUS.md for why).

## Environment variables

See `.env.example`. Nothing is required to run locally; `CHORDFORGE_DATA_DIR`, `COOKIE_SECURE`, and
`TRUST_PROXY` matter for deployment -- see `docs/DEPLOYMENT.md`.

## Testing

`python3 -m tests.run_all` runs the full suite: audio decode/reject edge cases, the mixing DSP chain
(auto-level, ducking, compression, EQ, crossfades, limiting) with measurable correctness checks,
mixdown-job lifecycle (queued/running/succeeded/failed, busy-state handling, crash/restart
recovery), storage security (path traversal, symlink escape), database per-user isolation, auth
security, multi-stem clips/song-structure, and full HTTP API/user-journey tests. The workflow has
also been verified end-to-end in a real headless browser (record/upload two clips, arrange a
section, build the song, confirm real playable audio comes back).

## Deployment

See `docs/DEPLOYMENT.md`. In short: any host that can run a long-lived Python process with a
persistent disk works; a reverse proxy/HTTPS terminator is expected in front of it.

## Known limitations

- The mix is a fixed chain with no user-adjustable parameters (no EQ curve, no compression ratio,
  no per-section mix settings) -- see "How the mix works" above for exactly what it does.
- Section length is always "fit to the longest part placed in it"; there's no way to set an
  explicit length or tempo grid.
- Single-process SQLite + local disk: fine for personal use; would need a real database/storage
  service to scale beyond one instance.

## Future improvements

Per-section mix adjustments (a duck-amount or EQ slider), a proper job queue for mixdown, stems
export (so you can take the balanced-but-unmixed parts elsewhere), tempo-synced looping instead of
natural-loop-point detection, real-time monitoring during recording (currently reference playback
has ordinary browser/OS audio latency, not sample-accurate sync).
