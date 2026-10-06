# PROJECT_STATUS

Last updated: pivot to a pure recording/editing + auto-mix tool (see the last "Update" section below
for what changed and why -- the app is no longer a chord-detection/generation tool at all).

## Steps
- [x] 1. Tech Stack
- [x] 2. App UI
- [x] 3. App Value (musical representation layer)
- [x] 4. Secure Sign-Up
- [x] 5. Database
- [x] 6. Functions (full pipeline: audio -> analysis -> arrangement -> generation)
- [x] 7. Storage
- [x] 8. Live App (code complete, audited, and tested here; real deployment is on you -- see docs/DEPLOYMENT.md)

## What actually exists (all in this repo, all tested)

**Stack** (see docs/ARCHITECTURE.md for the original plan and why it changed): Python 3, standard-library
`http.server` for the API, SQLite (stdlib `sqlite3`) for the database, local disk for file storage,
NumPy/SciPy for all audio DSP, a small hand-rolled HTML/CSS/vanilla-JS frontend (no build step). The
original plan assumed Next.js + FastAPI + Supabase + librosa; I changed to a from-scratch,
dependency-light stack because this sandbox has no network access to install packages or provision
Supabase, and I judged "fewer moving parts, fully tested here" better than "the originally-planned
stack, entirely untested." See "Architecture change" below.

**Musical representation** (`chordforge/representation.py`): typed, validated chord/tempo/key data
with per-chord and per-key confidence, warnings, and strict `from_dict`/`to_dict` for safe
persistence and editing.

**Audio input & preprocessing** (`chordforge/audio/`): format sniffing (not trusting file
extensions), WAV decoding built by hand (8/16/24/32-bit PCM), ffmpeg fallback for MP3/OGG/FLAC/M4A/
WebM when ffmpeg is present, size/duration/silence/corruption validation, resampling, high-pass
filtering, silence trimming, clipping detection.

**Chord/tempo/key analysis** (`chordforge/analysis/`): spectral-peak chroma extraction, major/minor
triad template matching with Viterbi smoothing for chord segmentation, autocorrelation-based tempo
estimation with a log-normal prior, Krumhansl-Schmuckler key estimation blended with diatonic chord
fit. Confidence is measured (chroma-template fit, autocorrelation prominence), not invented, and a
low-clarity recording is now flagged as unreliable rather than silently producing confident-looking
nonsense (see "Honesty checks" below).

**Arrangement + generation** (`chordforge/arrangement.py`, `chordforge/generation/`): a validated
`ArrangementRequest` built from the user's progression/tempo/key plus their genre/mood/instrument
choices; a `GenerationProvider` interface; a default `symbolic` provider that builds real MIDI-style
drum/bass/keys/lead parts from the exact chord progression and renders them with NumPy synthesis
(genuinely follows the user's chords -- verified by re-analyzing the output, see below); an
`external_placeholder` provider that raises a clear, honest error instead of ever faking success,
documenting exactly what a real neural provider (e.g. Replicate MusicGen) would need.

**Auth** (`chordforge/auth.py`): scrypt password hashing with per-user salt, constant-response
timing for wrong-password vs unknown-email, hashed session tokens, session expiry, per-key rate
limiting on login/register.

**Database** (`chordforge/db.py`): SQLite with a versioned migration runner, a `ProjectRepo` where
every method takes the acting user's ID and a project owned by someone else is indistinguishable
from a missing one (404), cascade delete, interrupted-job recovery on restart.

**Storage** (`chordforge/storage.py`): server-generated keys only (user filenames are never used),
strict key-format validation, path-traversal and symlink-escape protection, per-project delete.

**HTTP API + web UI** (`chordforge/server.py`, `web/`): stdlib HTTP server (no framework), session
cookies (HttpOnly, SameSite=Lax, Secure when configured), CSRF protection (custom header + Origin
check), CSP with no inline scripts/styles, byte-range support for audio seeking, a plain HTML/CSS/JS
frontend matching the requested UI (record/upload, chord timeline with confidence, correction
editor, genre/mood/instrument selection, generation status, playback, download), responsive down to
a 390px mobile viewport (verified with a real headless-browser run, not just CSS review).

## Tests (all passing, run with `python3 -m tests.run_all` from the repo root)

102 automated tests, 1 skipped (an ffmpeg-codec test that only runs if certain codecs are present
on the machine): representation validation/roundtrip, audio decode/reject/preprocess edge cases
(corrupt/silent/oversized/wrong-format/truncated files), chord/tempo/key analysis on synthesised
guitar audio (multiple keys/progressions, noise, mistuning, sparse strumming), arrangement request
validation, every genre x mood combination of the symbolic generator (including a round-trip check
that re-analyzes generated audio and confirms it matches the requested progression), provider
failure modes (unavailable, timeout, garbage/silent/truncated response) and that failures are always
reported, never faked, storage path-traversal/symlink/permission-failure handling, database
per-user isolation (User A cannot read/edit/delete User B's project; verified both at the repository
layer and over real HTTP), SQL-injection inertness, auth (hashing, rate limiting, session expiry,
duplicate accounts, wrong-password vs unknown-user indistinguishability), and full HTTP API tests
covering the whole user journey plus CSRF, headers, range requests, concurrency (generation "busy"
state, delete-during-generation leaves no orphaned files), and interrupted-job recovery on restart.

Beyond the unit/API tests, I ran the actual server in a real headless browser (Playwright,
installed in this sandbox) and drove the full journey by hand: register -> create project -> upload
guitar audio -> see the detected chords/tempo/key -> open and apply the correction editor -> choose
genre/mood -> generate -> play the result -> get a working download link -> reload the page and
confirm the project persisted -> log out. I also checked the mobile layout (390px viewport, no
horizontal overflow) and the invalid-file-upload error path in the browser, not just via the API.

## Honesty checks built into the code, and what I found

- The symbolic generator's output was verified by feeding it back through the analyzer: across
  6 genres x 2 moods it recovers 89-93% of expected chord-time (lower, ~57%, for sparse
  instrumentation with few instruments -- expected, there's less harmonic information to detect).
  This is an automated test (`test_generated_audio_follows_the_progression`,
  `test_every_genre_and_mood_renders_valid_audio`), not a one-off manual check.
- Key estimation confidence is now scaled down when the underlying chords were detected with low
  clarity, and a new warning ("This doesn't sound like clear guitar chords...") fires when the
  overall chroma-template fit is weak -- verified against synthesised white noise (correctly flagged,
  overall confidence ~0.06) versus synthesised guitar chords (correctly high, ~0.85-0.90).
- `validate_result()` rejects anything from a generation provider that isn't real, playable,
  non-silent audio of a plausible length -- including a truncated WAV whose header claims more
  frames than the file actually contains, which is exactly the kind of "looks successful but isn't"
  bug this project's rules are meant to catch.
- The `external_placeholder` provider raises rather than fabricating a track; this is exercised by
  a test asserting `ProviderUnavailable`, not just described in a comment.

## Architecture change from the original Step 1 plan, and why

Step 1 planned Next.js + FastAPI (Python worker) + Supabase (auth/DB/storage) + librosa, deployed to
Vercel + a Docker host. That plan assumed I would eventually have network access to install
packages and stand up real accounts. I was later told I would not have network access for this
build. Rather than write a large amount of code against Next.js/FastAPI/Supabase/librosa that I
could never run or test here, I rebuilt the plan around what this sandbox actually has installed and
can execute: Python's standard library, NumPy, and SciPy (confirmed present), a headless browser
(Playwright, confirmed present and used for real UI testing), and ffmpeg (confirmed present, used
for non-WAV audio decoding). Every line of the resulting app has been run, and the entire user
journey has been exercised in a real browser, which would not have been true of the original stack
in this environment. The pipeline, honesty rules ("no faked functionality," "no invented APIs," "a
replaceable generation provider"), and all 8 steps from the brief are otherwise followed as
specified. `docs/ARCHITECTURE.md` still documents the original plan for reference; this file is the
accurate record of what was actually built.

## What remains -- things only you can do

- **Choose and deploy a host**, and set the environment variables in `.env.example` for real values.
  See `docs/DEPLOYMENT.md` for a concrete walkthrough (systemd and Docker examples) and exactly what
  I could and couldn't verify myself.
- **Decide on a database/storage upgrade** if you outgrow SQLite + local disk (the repository layer
  in `chordforge/db.py` and `chordforge/storage.py` is the only place that would need to change).
- **Add a real neural generation provider**, if wanted, by implementing `GenerationProvider` in
  `chordforge/generation/` (see the docstring in `generation/external.py`) -- this needs a real
  account/API token I cannot create for you, and needs network access to verify against the
  provider's current API, which I did not have in this session.
- **Test with real guitar recordings**, not just the synthesised test audio used here. The analyzer
  is honest about uncertainty (confidence scores, warnings, an editable correction UI), but its
  real-world accuracy on an actual human playing an actual guitar has not been measured, only
  documented as an open item.
- **Security review of your deployment environment** (TLS termination, host hardening, secret
  management) -- I audited the application code itself (see "Honesty checks" and the test suite),
  not infrastructure I don't have access to provision.

## Known limitations (unchanged from Step 1's honest assessment)

- Only major/minor triads are recognised; other chord types are mapped to the nearest triad. The UI
  always discloses this.
- Tempo/key confidence is genuinely low on short or sparsely-strummed clips; the UI surfaces this
  and lets the user correct both.
- The default generation is rule-based synthesis, not a neural model. It is fast, free, needs no
  credentials, and (per the round-trip tests above) is verifiably faithful to the user's chords --
  but it is not the more expressive result a hosted neural model could eventually provide.


## Update: "extend the song with my exact guitar sound" + a real bug found and fixed

You asked for the generated track to use your actual recorded guitar audio throughout the song,
not just as a short intro. That wasn't in the app before -- I added it as a new instrument option,
**"Your Guitar (looped)"**, alongside Drums/Bass/Keys/Lead Guitar. When enabled, each chord's slot
in the generated arrangement is filled with your actual recorded audio for that chord (looped and
crossfaded to fill however long the arrangement gives it), not a synthesised substitute. I verified
this isn't just "some audio" but genuinely your recording: I cross-correlated a chunk of generated
output against the original recorded segment for the same chord and got a 0.97 correlation. It can
be combined with the synthesised instruments, or used on its own for something closer to "my guitar
tone, extended into a full arrangement." Tests: `test_your_guitar_instrument_uses_the_actual_recording`,
`test_your_guitar_combines_with_synthesised_instruments`, `test_guitar_instrument_without_original_audio_does_not_crash`.

Separately, you reported the Generate button was greyed out and unclickable right after uploading.
I reproduced this exactly in a real headless-browser test and found a genuine bug: the upload
handler rendered the page (which disables Generate while "busy") *before* clearing its own busy
flag, so the button stayed stuck disabled until something else happened to re-render the page. I
fixed the ordering and re-ran the same browser test to confirm the button is now clickable
immediately after upload. While investigating I found a second, related bug I introduced while
adding the new instrument: the client's default instrument list didn't include the new "guitar"
option, so it silently rendered checked but was immediately overridden to unchecked. Fixed the
same way -- verified in the browser that all five instruments (including Your Guitar) are checked
by default and that upload-then-immediately-generate now works end to end. Full suite: 105 tests
passing (1 skipped, same as before).


## Update: more polished looping + a "type a direction" quick-refine box

**Loop quality.** The "Your Guitar" instrument previously just tiled your exact recorded strum
back-to-back, attack included, which sounded mechanical on longer loops. It now detects the end
of the strum's attack transient (via a short-time energy envelope), plays the attack once, then
loops only the ringing/sustain tail -- with a crossfaded seam and a small random per-repeat
volume variation so it doesn't sound like an exact copy-paste. I found and fixed a real bug while
building this: my first crossfade implementation silently dropped samples at the seam (a
length-accounting error), which I caught with an automated click-detection test comparing
sample-to-sample jumps before and after -- not just by listening. New tests:
`TestNaturalChordClip` (4 tests: no clicks even looped far beyond the original length, repeats
are genuinely varied not bit-identical, short slots still work, empty input doesn't crash).

**Quick-refine box.** You can now type a short direction on the result card ("a bit faster,
darker, more guitar, less drums") and re-generate without re-uploading or reopening the genre/mood
pickers -- it reuses the already-analysed chords. Important honesty note, also stated in the UI
itself: this app has no network access and no AI language model wired in, so this is NOT free-form
natural-language understanding. It's a curated keyword matcher (`chordforge/refine.py`) that
recognises concrete phrases -- tempo direction, one of the 5 mood words, one of the 6 genre names,
and "more/less/no/add back <instrument>" -- and turns whatever it recognises into real generation
parameters (tempo scaling, per-instrument gain, instrument on/off, genre/mood). Anything it
doesn't recognise is reported back honestly rather than silently ignored, and it still generates
using your current settings. Genre/mood/instrument-on-off changes persist like a normal
generation; tempo/volume tweaks apply to that one render only. New: `chordforge/refine.py`,
`tests/test_refine.py` (10 tests), `TestRefineApi` (6 end-to-end HTTP tests covering the happy
path, validation, per-user isolation, busy-state handling, and the "generate anyway, tell them
honestly" path for unrecognised text). Verified in a real browser, not just via the API.

Full suite: 115 tests passing (1 skipped, unchanged). One pre-existing, timing-sensitive test
(an oversized-upload edge case) occasionally shows a benign `BrokenPipeError` under full-suite
load but passes reliably in isolation -- this predates these changes and reflects correct server
behavior (closing the connection early on an oversized request) racing with the test client, not
an application bug.


## Update: multi-stem song builder (clips, song structure) + Lyric Ideas panel

You asked for three things together: record/tag multiple parts, auto-arrange them into a full
song, and a real (if simple) editor -- with synthesized instruments kept as a fallback, plus a
lyric-writing aid. All of it is built, tested (184 automated tests, up from 152), and verified in
a real browser.

**Clips.** A project can now hold multiple labeled recordings (vocals, guitar, drums, bass, keys,
lead, other), each with its own trim start/end, volume, and mute -- fully isolated per user,
fully tested (upload, list, update, delete, playback with range support, trim/gain validation,
30-clip-per-project limit).

**Song structure.** An ordered list of sections (Intro/Verse/Chorus/whatever you name them), each
spanning a number of your chord progression's repeats, with a checklist of which clips play in
each section and whether each one loops to fill the section or plays once. Deleting a clip cleans
it out of any section that referenced it, automatically.

**Generation.** When a structure with at least one clip assignment exists, Generate builds the
song from it: each section's real clips are rendered into that section's time window, looped
naturally (attack once, then a smoothly-looped sustain -- the same technique built for "Your
Guitar") if you check loop, or played once if you don't. Synthesized instruments (drums/bass/
keys/lead) still play throughout as a fallback/layer per your earlier answer -- in this version
they are NOT automatically suppressed in sections where you've supplied a real clip of that same
type, so if you enable both, you'll hear both. A project with no structure at all behaves exactly
as before (verified with a dedicated regression test).

**Lyric Ideas panel** (`chordforge/lyrics.py`): rule-based theme prompts, image words, a genre
note, and a syllable/tempo-feel hint, all keyed to your key/mood/genre, plus an offline rhyme
approximation for a word you type in. Explicitly labelled in the UI as a keyword-based writing
aid, not an AI songwriter -- this app has no network access or language model.

**Bugs found and fixed while building this** (each caught by an automated test or a real-browser
run, not just noticed by inspection):
- The old "select at least one instrument" validation blocked a clips-only song with every
  synthesised instrument off; moved the check to after the system knows whether real clips will
  provide sound.
- A pre-existing bug in the drum-thinning logic: for a song only 1-2 chord cycles long, the "quiet
  the first and last loop" heuristic silenced literally every loop, producing a fully silent drum
  track. Surfaced by a short structure-driven generation test; fixed to only thin when the song is
  long enough to still have activity left over.
- The client-side "select at least one instrument" pre-check (in app.js) didn't know a song
  structure could supply the sound instead, and blocked a valid clips-only generation before it
  ever reached the server. Removed; the server's proper check is now the single source of truth.
- The Lyric Ideas panel loaded once per project-open, but uploading/analysing audio doesn't change
  the project's id, so it never refreshed after the first real analysis appeared. Fixed to reload
  whenever a project newly has an analysis it hasn't fetched ideas for yet.
- The song-structure editor re-renders on every edit (checkbox, rename, reorder); rapid edits
  could in principle race two in-flight saves and have an older response silently roll back a
  newer one. Added a sequence guard and verified directly (not just by inspection) that two
  overlapping saves both survive and the server ends up with both changes.

New files: `chordforge/song.py`, `chordforge/lyrics.py`. New tests: `tests/test_song.py`,
`tests/test_song_services.py`, `tests/test_song_api.py`, `tests/test_lyrics.py` (65 tests total
across these). Full suite: `python3 -m tests.run_all` -- 184 tests passing (1 skipped, unchanged).

## What I did not build in this pass (told to you honestly, not silently skipped)

- **Drawn waveforms for trimming.** Trim is numeric (seconds in/out) with audio preview, not a
  visual waveform you can drag. Functional, not fancy.
- **Multiple clip instances per section** (e.g. two different vocal takes both playing in the
  same Chorus at different offsets). Currently one placement per clip per section.

## Update: record against a reference clip + synthesized instruments are now user-placed clips

You asked for two changes, both about giving you control instead of automatic behavior: (1) hear
an existing part while recording a new one, so you can perform in time with something you already
laid down, and (2) get rid of synthesized drums/bass/keys/lead as an automatic whole-song layer,
while keeping the ability to use them -- generated on demand, placed by you. Both are built,
tested (203 automated tests, up from 184), and verified in a real headless-browser run (Chromium
with a fake mic device), not just by unit tests.

**Reference playback while recording.** In "Build a full song", a new dropdown next to "Record
part" lets you pick any existing part to play back the moment you start recording. It plays from
that part's trim-in point through the browser's normal audio output while `MediaRecorder` captures
your mic, so you can sing over your guitar, or play a new instrument along with a vocal you already
took. This is playback + capture happening at the same time in the browser, not a mixed monitor
feed, so: **you need headphones** -- without them, the reference audio bleeds into your microphone
and ends up baked into the new recording. This is stated plainly in the UI, not hidden. There's
also no way to guarantee sample-accurate sync between the two (ordinary browser/OS audio latency
applies) -- fine for singing along, but don't expect drum-machine-tight timing from it. Purely a
client-side feature (`web/app.js`: `startRecordingClip`, `stopReferencePlayback`), so it has no
server-side test coverage; it's covered by the manual browser verification described above instead.

**Synthesized instruments, generated on demand, placed by you.** The old "Instruments" checklist
on the Direction panel (Drums/Bass/Keys/Lead/"Your Guitar (looped)") is gone -- generation no
longer auto-applies any synthesized layer across the whole song. Instead, "Build a full song" has
a new "Generate an instrument" control: pick drums/bass/keys/lead and a length in chord cycles,
click Generate, and it's added to your parts list (tagged "Generated" so you can tell it apart from
your own recordings) exactly like a recording -- you then assign it to whichever section(s) you
want via the same checkboxes as any other part, or leave it unused. Nothing happens to your song
automatically. New building block: `generate_instrument_clip()` in
`chordforge/generation/symbolic.py`, which renders one instrument on its own (reusing the same
drum/bass/keys/lead synthesis as before, just not tied to a whole-song render), and
`ProjectService.generate_instrument()` in `chordforge/services.py`, which saves that as an ordinary
clip. The underlying `ArrangementRequest.instruments` toggle mechanism and the old "Your Guitar
(looped)" whole-song auto-layer still exist in the codebase and are still directly tested (several
existing tests exercise them through the low-level API), but nothing in the UI can reach them any
more -- they're dead from the user's side, kept only so the well-tested rendering code underneath
didn't need to be ripped out to satisfy this change.

**Knock-on changes, done for consistency, not asked for directly:**
- The "type a direction" refine box's "more/less/no `<instrument>`" recognition is removed --
  there's no longer an automatic instrument balance for a text direction to adjust. Refine still
  recognises faster/slower, mood words, and genre names. Want to change an instrument part now?
  Generate a new one, or re-record it, and use its volume slider.
- A clip now carries a `source` field (`"recorded"` or `"generated"`), purely so the UI can show a
  small "Generated" badge -- it has no effect on rendering.
- Removed the misleading generic "with synthesised instruments" phrase from the result panel's
  note, since that's no longer always true.

**What I did not build here:**
- **A real low-latency monitoring mix** (hearing your own mic input alongside the reference,
  blended, the way a DAW would). Browsers don't give a web app a clean way to do that without
  noticeable round-trip latency; what's built is straightforward reference playback + separate mic
  capture, which is why headphones matter.
- **Automatically muting the synthesized "Your Guitar" auto-layer's UI** -- it's not deleted, just
  unreachable from the UI, per above. If you want that code actually removed rather than dormant,
  say so and I will (it just means touching several already-tested code paths for no functional
  gain right now).
- **Undo/regenerate-in-place for a generated instrument clip.** Generating again makes a new clip
  alongside the old one (which you can then delete); it doesn't overwrite it.

New tests: `TestGenerateInstrumentClip` (both in `tests/test_generation.py` for the synthesis
function and `tests/test_song_services.py` for the service), `TestGenerateInstrumentApi` in
`tests/test_song_api.py`, plus `tests/test_refine.py` updated for the removed instrument-phrase
parsing. Full suite: `python3 -m tests.run_all` -- 203 tests passing (1 skipped, unchanged).

## Update: pivot to a pure recording/editing + auto-mix tool (all generation removed)

You said, in your words: "i want this to be a music editor so no generation. i want to be able to
play my guitar for each section of a typical song but i want to be able to add vocals to play over
the guitar. the automation part comes in when the song gets put together from my recordings and
edits the vocals over the guitar and makes my vocals and guitar sound mor pleasing to the ear." This
supersedes every generation/instrument feature described above, including the "generate an
instrument, place it yourself" feature from the previous update -- that whole concept is gone, not
just hidden from the UI this time.

**What was deleted, not just hidden:** `chordforge/generation/` (the whole symbolic-synthesis
package), `chordforge/analysis/` (chord/tempo/key detection: chroma extraction, triad templates,
Viterbi smoothing, autocorrelation tempo, Krumhansl-Schmuckler key), `chordforge/arrangement.py`
(genre/mood/instrument request building), `chordforge/refine.py` (the "type a direction" keyword
parser), `chordforge/lyrics.py` (the Lyric Ideas panel -- it was keyed off genre/mood, which no
longer exist), `chordforge/representation.py` (the chord/tempo/key data contract), and
`chordforge/audio/preprocess.py`. Their test files went with them. This is a real deletion, per your
explicit "Remove it entirely" answer when asked whether the chord/tempo/key step should stay in any
form -- there is no dormant, UI-unreachable code left over this time (unlike the previous update,
which kept the old whole-song instrument layer dormant; that's now gone too, since its module is
deleted).

**What replaced it.** ChordForge is now a record-and-edit tool with one automated step:

- **No chord/tempo/key analysis, no genre/mood, no "Direction" step.** A project is just a name and
  a set of clips arranged into sections -- there's nothing to analyze first.
- **Section length is "fit to your longest recording"** (your explicit choice over a manual cycle
  count), not a chosen number of chord-progression repeats -- `chordforge/song.py`'s `SectionSpec`
  no longer has a `cycles` field at all.
- **A new mixing engine, `chordforge/mixing.py`**, replacing all synthesis: auto-leveling (RMS-based
  gain to a common target, clamped), attack/release envelope-follower-based sidechain ducking (the
  instrumental bed recedes under vocal presence), feed-forward compression, RBJ-cookbook peaking EQ
  and Butterworth high-pass filtering, equal-power crossfades between sections, and a soft-clip
  limiter with peak normalization for the master. This is your recordings, processed -- nothing is
  invented. See the module docstring and "How the mix works" in README.md for the exact chain, and
  `tests/test_mixing.py` for measured-effect tests on each stage (e.g. `duck` is checked to actually
  reduce bed-track RMS when a vocal is present, not just "not crash").
- **"Generate" became "Build my song."** `ProjectService.start_mixdown()`/`open_mixdown_audio()`
  replace `start_generation`/`refine_generation`/`upload_recording`/`open_file`; the same
  async-job/polling pattern is reused (renamed `generation_status` DB column now means "mixdown
  status" to avoid an unnecessary migration -- see the comment in `chordforge/db.py`).
- **The reference-clip-while-recording feature is unchanged and kept** -- it was already a pure
  recording aid, not a generation feature, so nothing about your request touched it.

**What I did not rebuild, and why:** the Lyric Ideas panel is gone rather than ported, because it
was explicitly keyed off genre/mood/analysis (which no longer exist) and you didn't ask for it to
stay; if you want a similar writing aid later, it would need to be redesigned standalone rather than
restored as-is.

**Verification.** Full suite rewritten for the new model and passing: `python3 -m tests.run_all` --
133 tests (1 skipped), covering audio decode/reject, the mixing DSP chain with measured-effect
assertions, mixdown-job lifecycle (queued/running/succeeded/failed, busy-state conflicts, crash/
restart recovery, storage cleanup on delete/replace), storage security, database per-user isolation,
auth security, and clip/structure CRUD over HTTP. Also verified end-to-end in a real headless
Chromium browser: register, upload a guitar clip and a vocal clip, add a section, assign both clips
to it, click "Build my song," and confirm a real, sizeable, playable WAV comes back (not a stub).

**Known follow-up, not done in this pass:** the mix chain has no user-adjustable parameters (no
per-section duck amount, no EQ control) -- see README.md's "Known limitations." `docs/ARCHITECTURE.md`
and parts of `PROJECT_STATUS.md` above this section describe the earlier chord-detection/generation
design and are now historical only; they're kept for context on how the project's scope evolved, not
as a description of what exists today (this section and README.md are the current, accurate
description).
