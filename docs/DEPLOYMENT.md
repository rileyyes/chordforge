# Deploying ChordForge

ChordForge is a single Python process (stdlib HTTP server + SQLite + local file storage). It has
no required external services in its default (`symbolic`) configuration, so it can run on any
host that can run Python 3.11+.

## What you need to do (I cannot do these for you)

1. **Choose a host.** Any VM/container host works (a small Fly.io, Railway, Render, or a plain VPS).
   A serverless platform (e.g. Vercel functions) is a poor fit: ChordForge keeps a SQLite file and
   local audio files on disk, and runs background generation jobs in-process, both of which need a
   long-lived, single persistent disk.
2. **Provision persistent storage.** Mount a persistent volume and point `CHORDFORGE_DATA_DIR` at it.
   If the disk is lost, projects and audio are lost — there is no cloud storage in the MVP config.
3. **Set environment variables** (see `.env.example`). At minimum:
   - `PORT` — the host usually injects this.
   - `CHORDFORGE_DATA_DIR` — path to the persistent volume, e.g. `/data`.
   - `COOKIE_SECURE=1` — once served over HTTPS (required in production).
   - `TRUST_PROXY=1` — if behind a reverse proxy/load balancer, so rate limiting sees the real client IP.
4. **Put TLS in front of it.** The app itself serves plain HTTP; use your platform's HTTPS termination
   or a reverse proxy (Caddy, nginx). `COOKIE_SECURE=1` requires HTTPS or the session cookie will be
   dropped by browsers.
5. **Install ffmpeg** on the host if you want MP3/OGG/FLAC/M4A/WebM upload support. Without it, only
   WAV uploads work (the app still functions, `/api/config` reports the reduced format list, and the
   UI's accepted-format hint should be checked against it).
6. **Run it as a service** that restarts on crash (systemd, a platform's process manager, Docker
   restart policy). On restart, any generation job left `queued`/`running` is automatically marked
   `failed` with an explanatory message (see `ProjectRepo.fail_interrupted`), so users are never shown
   a job that silently vanished.

## Example: plain VPS with systemd

```
python3 -m venv /opt/chordforge/venv
/opt/chordforge/venv/bin/pip install -r requirements.txt   # see below; stdlib + numpy/scipy only
```

`requirements.txt` (create at repo root):
```
numpy>=1.26
scipy>=1.11
```

`/etc/systemd/system/chordforge.service`:
```
[Unit]
Description=ChordForge
After=network.target

[Service]
User=chordforge
WorkingDirectory=/opt/chordforge/app
EnvironmentFile=/opt/chordforge/chordforge.env   # holds the vars from .env.example, real values
ExecStart=/opt/chordforge/venv/bin/python3 -m chordforge
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Put a reverse proxy (Caddy is simplest for automatic HTTPS) in front of port 8000 (or your `PORT`).

## Example: Docker

```
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chordforge chordforge
COPY web web
ENV CHORDFORGE_DATA_DIR=/data PORT=8000
VOLUME /data
EXPOSE 8000
CMD ["python3", "-m", "chordforge"]
```

Run with a mounted volume: `docker run -p 8000:8000 -v chordforge_data:/data --env-file chordforge.env chordforge`.

## What I verified myself vs. what needs you

I ran the full app (server + browser) inside this sandbox and confirmed registration, project
creation, upload, analysis, correction, generation, playback, download, persistence across a
reload, and logout all work, plus the full automated test suite (102 tests) passes. I could not
verify: behavior behind a real reverse proxy/TLS terminator, behavior under your platform's process
manager/restart policy, or performance/behavior at real user volumes. Please smoke-test those after
deploying.

## Known limitations to keep in mind after deploying

- Single-process, in-memory rate limiting: if you run more than one instance behind a load balancer,
  each instance rate-limits independently. Fine for an MVP; note it if you scale out.
- SQLite is a single-writer database: fine for an MVP's traffic, but it is not built for many
  concurrent instances writing to the same file. If you outgrow it, migrate to Postgres (the
  `db.py` repository layer is the only place that would need to change).
- The `symbolic` generation provider is CPU-bound Python/NumPy; a generation request runs on a
  small thread pool (`GENERATION_WORKERS`, default 2). Size your host's CPU accordingly, or lower
  concurrent load.
