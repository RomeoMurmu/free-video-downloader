# OmniDownloader

Flask + yt-dlp based downloader for content that you own or are authorized to download. Platform terms and copyright rules must be respected.

## Features

- HTTP/HTTPS URL validation
- Safe file serving without path traversal
- Rate limiting on metadata and download endpoints
- Accurate available video qualities and metadata
- FFmpeg detection for MP3 conversion
- Background downloads with progress polling
- Automatic one-time cleanup after the file response closes
- Periodic orphan-file cleanup (default: 15 minutes)

## Setup

```bash
cd ~/Desktop/video-downloader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

FFmpeg is required for MP3 conversion:

```bash
sudo apt install ffmpeg
```

## Run

```bash
python app.py
```

Open http://127.0.0.1/.

For a production-style local run:

```bash
gunicorn --workers 2 --bind 0.0.0.0:5000 app:app
```

## Deploy on Render

Create a Render Web Service connected to this repository.

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn --workers 2 --threads 4 --timeout 300 --bind 0.0.0.0:$PORT app:app`
- Environment: Python 3

Alternative: choose Docker as the Render environment to keep MP3 conversion available. The included `Dockerfile` installs FFmpeg and starts Gunicorn using Render's `$PORT`.

Do not hardcode port 80 on Render. Render provides the `$PORT` environment variable, and the app already reads it. Add `TEMP_FILE_MAX_AGE` and `CLEANUP_INTERVAL` as optional environment variables if needed.

The local `downloads/` folder is ephemeral on Render. Files are intended to be temporary and are automatically removed after serving. Do not use it for permanent storage. For MP3 conversion, make sure the deployment image includes FFmpeg; otherwise disable MP3 or use a custom Docker deployment with FFmpeg installed.

## Test

```bash
pytest -q
```

## API flow

1. POST `/info` with `{ "url": "https://..." }` to inspect available formats.
2. POST `/download-format` with URL, quality, and type. It returns a `job_id`.
3. Poll GET `/progress/<job_id>` until `status` is `completed` or `failed`.
4. On completion, open the returned `download_url`.

The built-in Flask server is for development only. A public deployment should add HTTPS, a reverse proxy, persistent job storage/queue, stronger SSRF policy, Redis-backed rate-limit storage, and scheduled cleanup of old downloads. `TEMP_FILE_MAX_AGE` and `CLEANUP_INTERVAL` can be set through environment variables (seconds).
