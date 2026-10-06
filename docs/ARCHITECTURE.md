# ChordForge Architecture (Step 1)

## 1. Decisions

| Area | Choice | Why | Replaceable? |
|---|---|---|---|
| Frontend | Next.js (App Router) + TypeScript + Tailwind | One framework for UI and thin API layer; MediaRecorder/Web Audio work well | Yes (UI talks to worker only via HTTP) |
| Web backend | Next.js route handlers | Auth checks, project CRUD, orchestration only. No audio work here | Yes |
| Audio processing + analysis | Separate Python service (FastAPI) with ffmpeg, librosa, NumPy | Python has the mature audio/MIR tooling | Yes (Analyzer interface) |
| Chord analysis (MVP) | Beat-synchronous chroma (librosa) + major/minor triad template matching + Viterbi smoothing | Simple, explainable, yields real confidence values. Will be imperfect | Yes: swap for a learned model later |
| Key / tempo | Krumhansl-Schmuckler key profiles on aggregate chroma; librosa beat tracker | Same reasoning. Tempo on ~8 s clips is unreliable, so UI must allow override | Yes |
| Music generation | Provider interface with two planned adapters: `symbolic` (default) and `replicate_musicgen` | See section 3 | Yes, by design |
| Auth | Supabase Auth | Handles hashing, sessions, recovery; avoids hand-rolled auth | Moderate |
| Database | Supabase Postgres, migrations via Supabase CLI, Row Level Security | Per-user isolation enforced in the database, not only in app code | Moderate |
| File storage | Supabase Storage (private buckets, signed URLs) | Same vendor as auth/DB keeps MVP simple | Yes (storage module wraps it) |
| Deployment | Web on Vercel; worker as a Docker container on any Docker host (Fly.io suggested) | Worker needs ffmpeg/fluidsynth, so it needs a container | Yes |
| Testing | Vitest + Testing Library + Playwright (web); pytest (worker) | Standard per ecosystem | Yes |

Deliberately excluded: message queue, Redis, GraphQL, microservice sprawl. Two deployables only: `web` and `audio-worker`.

## 2. Pipeline

```
Guitar Audio (record/upload)
  -> Storage (private bucket)                                [web + storage module]
  -> Preprocess (decode, mono, resample, trim, normalize)    [worker]
  -> Musical Analysis (chords, tempo, key, confidence)       [worker]
  -> Musical Representation (typed, versioned JSON)          [worker -> DB]
  -> User review/correction + genre/mood/instruments         [web]
  -> Arrangement Request                                     [worker]
  -> Generation Provider (symbolic | replicate_musicgen)     [worker]
  -> Generated audio -> Storage -> DB reference
  -> Playback / Download (signed URLs)                       [web]
```

The Musical Representation is the contract between analysis and generation. Each side depends on its schema, not on the other.

## 3. Music generation: honest options

The user's guitar idea must be the foundation. Options considered:

1. **Symbolic arrangement + synthesis (default MVP).** Build MIDI parts (drums, bass, keys, lead) from the detected chords/tempo/key using genre/mood rules, render with FluidSynth and a SoundFont, optionally layering the original recording. Works offline, no credentials, faithful to the chords by construction. It is rule-based, not neural, and will sound like a competent backing track rather than a produced song.
2. **Hosted neural model (adapter).** MusicGen-melody via Replicate is the leading candidate because it supports audio conditioning. Requires `REPLICATE_API_TOKEN`, costs money per run, and follows the input loosely (melody/chroma, not exact chords). Exact model identifiers and parameters must be verified against current provider docs in Step 6; nothing is assumed here.
3. Other services (Stable Audio, ElevenLabs Music, Lyria, etc.) could become adapters later if their current APIs support audio conditioning. Not verified.

Interface (worker, Python):

```
class GenerationProvider(Protocol):
    name: str
    def generate(self, request: ArrangementRequest, timeout_s: int) -> GenerationResult: ...
```

Errors: `ProviderUnavailable`, `ProviderTimeout`, `InvalidProviderResponse`. A provider that cannot run raises; it never returns fake audio.

## 4. Repository structure

```
chordforge/
  apps/web/                 Next.js app
  services/audio-worker/    FastAPI service
    app/api/                HTTP routes (shared-secret auth)
    app/audio/              input validation, preprocessing
    app/analysis/           chords, tempo, key (Analyzer interface)
    app/representation/     pydantic MusicalRepresentation
    app/arrangement/        ArrangementRequest builder, symbolic arranger
    app/generation/         providers (symbolic, replicate_musicgen)
    app/storage/            storage client wrapper
    tests/
  supabase/migrations/      SQL schema + RLS (Step 5)
  docs/
  scripts/
  PROJECT_STATUS.md  README.md  .env.example
```

## 5. Dependencies (unpinned here; pin via lockfiles at install)

Web: next, react, typescript, tailwindcss, @supabase/supabase-js, @supabase/ssr, zod, vitest, @testing-library/react, playwright, eslint, prettier.

Worker: fastapi, uvicorn, pydantic, numpy, librosa, soundfile, mido (or pretty_midi), httpx, supabase client or plain HTTP, pytest, ruff.

System (worker container): ffmpeg, fluidsynth, a SoundFont whose license is checked in Step 6.

## 6. Data flow and ownership

1. Browser uploads to Supabase Storage at `recordings/{user_id}/{project_id}/{random_uuid}.{ext}`. User filenames are never used.
2. Browser calls `POST /api/projects/{id}/analyze`. Next.js verifies session and ownership, then calls the worker with the storage key, authenticated by `AUDIO_WORKER_SHARED_SECRET`.
3. Worker downloads, validates, analyzes, writes the representation to the DB (service role, scoped to that project).
4. Generation is a job: `POST /api/projects/{id}/generate` creates a `generation_jobs` row (`queued -> running -> succeeded|failed`). The worker updates it; the browser polls. A real queue is a later upgrade.
5. Output goes to `generated/{user_id}/{project_id}/{uuid}.wav`; only the key is stored in Postgres. Playback/download use short-lived signed URLs.

## 7. Security considerations

- Secrets only in environment variables; `.env*` gitignored; `.env.example` has no values.
- Browser sees only the Supabase URL and anon key. Service-role key and worker secret are server-only.
- Authorization enforced twice: Postgres RLS (`user_id = auth.uid()`) and ownership checks in route handlers.
- Private buckets; path prefix must equal the caller's user ID.
- Upload validation: extension allowlist AND content sniffing (ffprobe), max size, min/max duration, reject empty/undecodable files.
- Worker requires the shared secret and should only be reachable from the web backend.
- Rate limiting on analyze/generate (cost and abuse control).
- Generic client error messages; details go to server logs only.
- `npm audit` and `pip-audit` in Step 8.

## 8. Testing strategy

- Worker unit: representation model, chord-template matching on synthesized triads (generated in tests, no large fixtures), key detection, validation, arrangement request builder, provider failure/timeout/invalid-response handling (mocked).
- Worker integration: analyze a synthesized progression end to end; check expected chords and reported confidence.
- Web: component tests, API route tests with mocked Supabase, Playwright journey in Step 8.
- Authorization: User A vs User B against a local Supabase instance.
- Accuracy is measured, not assumed: keep a small labeled set of real guitar clips and report results in PROJECT_STATUS.md.

## 9. Roadmap

Steps 2 to 8 as specified. Post-MVP: 7th/sus chords, learned chord model, stems, MIDI export, better neural generation, song sections, queue-based jobs.

## 10. Limitations to carry forward

- Chord recognition from a solo guitar clip will sometimes be wrong; the UI shows confidence and lets the user edit chords.
- Tempo/key from ~8 s of audio is low-confidence; allow manual override.
- Symbolic generation is not neural AI; neural generation depends on a paid external service and follows the input loosely.
