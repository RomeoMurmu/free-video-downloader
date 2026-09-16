import atexit
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, render_template, request, send_from_directory
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_FOLDER = BASE_DIR / "downloads"
DOWNLOAD_FOLDER.mkdir(exist_ok=True)
TEMP_FILE_MAX_AGE = int(os.getenv("TEMP_FILE_MAX_AGE", "900"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "300"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
limiter = Limiter(key_func=get_remote_address, app=app, default_limits=["60 per minute"])

JOBS = {}
JOBS_LOCK = threading.Lock()
QUALITY_LIMITS = {"4K": 2160, "2K": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
URL_SCHEMES = {"http", "https"}


def quality_limit(quality):
    if quality in QUALITY_LIMITS:
        return QUALITY_LIMITS[quality]
    match = re.fullmatch(r"([1-9][0-9]{1,3})p", str(quality or ""))
    if not match:
        return None
    height = int(match.group(1))
    return height if 1 <= height <= 4320 else None


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme in URL_SCHEMES and bool(parsed.netloc) and not parsed.username and not parsed.password


def ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def ytdlp_info_options():
    return {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "js_runtimes": {"node": {}},
        "impersonate": "chrome",
        "extractor_args": {"youtube": {"player_client": ["web_safari", "web_embedded", "android_vr"]}},
    }


def user_facing_extractor_error(exc):
    message = str(exc)
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered or "cookies-from-browser" in lowered:
        return "This platform is asking for verification right now. Please try another supported URL or try again later."
    return message


def format_size(size):
    if not size:
        return None
    units = ["B", "KiB", "MiB", "GiB"]
    size = float(size)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024


def quality_label(height):
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K"
    return f"{height}p"


def build_formats(info):
    formats = {}
    for item in info.get("formats", []):
        height = item.get("height")
        if not height or item.get("vcodec") == "none":
            continue
        label = quality_label(height)
        candidate = {
            "quality": label,
            "type": "video",
            "height": height,
            "ext": item.get("ext"),
            "fps": item.get("fps"),
            "has_audio": item.get("acodec") not in (None, "none"),
            "filesize": format_size(item.get("filesize") or item.get("filesize_approx")),
        }
        previous = formats.get(label)
        if previous is None or (candidate["has_audio"], candidate["height"]) > (previous["has_audio"], previous["height"]):
            formats[label] = candidate

    ordered = sorted(formats.values(), key=lambda item: item["height"], reverse=True)
    if any(item.get("acodec") not in (None, "none") for item in info.get("formats", [])) and ffmpeg_available():
        ordered.append({"quality": "MP3 (Audio)", "type": "mp3", "ext": "mp3", "bitrate": "192 kbps"})
    return ordered


def cleanup_file(path):
    try:
        candidate = Path(path).resolve()
        if candidate.parent == DOWNLOAD_FOLDER.resolve() and candidate.is_file():
            candidate.unlink()
            return True
    except OSError:
        pass
    return False


def cleanup_expired_files():
    cutoff = time.time() - TEMP_FILE_MAX_AGE
    for path in DOWNLOAD_FOLDER.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                cleanup_file(path)
        except OSError:
            continue


def cleanup_loop():
    while True:
        cleanup_expired_files()
        time.sleep(CLEANUP_INTERVAL)


cleanup_expired_files()
threading.Thread(target=cleanup_loop, daemon=True, name="download-cleanup").start()


def job_snapshot(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def set_job(job_id, **values):
    with JOBS_LOCK:
        JOBS[job_id].update(values)


def download_worker(job_id, url, quality, file_type):
    output_template = str(DOWNLOAD_FOLDER / f"{job_id}-%(title)s.%(ext)s")

    def progress_hook(data):
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            percent = round(downloaded * 100 / total, 1) if total else None
            set_job(job_id, status="downloading", progress=percent)
        elif data.get("status") == "finished":
            set_job(job_id, status="processing", progress=100)

    options = {
        **ytdlp_info_options(),
        "outtmpl": output_template,
        "restrictfilenames": True,
        "progress_hooks": [progress_hook],
    }
    if file_type == "mp3":
        if not ffmpeg_available():
            raise RuntimeError("FFmpeg is required for MP3 conversion but was not found.")
        options.update({
            "format": "ba/b",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        limit = quality_limit(quality)
        if not limit:
            raise ValueError("Unsupported video quality.")
        options["format"] = f"bestvideo[height<={limit}]+bestaudio/best[height<={limit}]"

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = Path(ydl.prepare_filename(info))
            if file_type == "mp3":
                filename = filename.with_suffix(".mp3")
            if not filename.is_file():
                matches = list(DOWNLOAD_FOLDER.glob(f"{job_id}-*"))
                if not matches:
                    raise FileNotFoundError("Downloaded file could not be located.")
                filename = max(matches, key=lambda path: path.stat().st_mtime)
        set_job(job_id, status="completed", progress=100, filename=filename.name, expires_at=int(time.time() + TEMP_FILE_MAX_AGE))
    except Exception as exc:
        set_job(job_id, status="failed", error=user_facing_extractor_error(exc))


@app.route("/robots.txt")
def robots():
    return app.response_class(
        "User-agent: *\nAllow: /\nDisallow: /info\nDisallow: /download-format\nDisallow: /progress/\nDisallow: /get-file/\nDisallow: /cleanup/\nSitemap: /sitemap.xml\n",
        mimetype="text/plain",
    )


@app.route("/sitemap.xml")
def sitemap():
    return app.response_class(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<url><loc>/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>'
        '</urlset>',
        mimetype="application/xml",
    )


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/info")
@limiter.limit("10 per minute")
def get_video_info():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not validate_url(url):
        return jsonify(success=False, error="Please provide a valid http(s) video URL."), 400
    try:
        with yt_dlp.YoutubeDL({**ytdlp_info_options(), "quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        return jsonify(success=True, title=info.get("title", "Downloaded Video"), duration=info.get("duration_string", ""), thumbnail=info.get("thumbnail", ""), formats=build_formats(info), url=url)
    except Exception as exc:
        return jsonify(success=False, error=user_facing_extractor_error(exc)), 502


@app.post("/download-format")
@limiter.limit("5 per minute")
def start_download():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    quality = data.get("quality")
    file_type = data.get("type", "video")
    if not validate_url(url) or (file_type == "video" and quality_limit(quality) is None) or (file_type == "mp3" and quality != "MP3 (Audio)"):
        return jsonify(success=False, error="Invalid download parameters."), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "progress": 0, "filename": None, "error": None}
    thread = threading.Thread(target=download_worker, args=(job_id, url, quality, file_type), daemon=True)
    thread.start()
    return jsonify(success=True, job_id=job_id), 202


@app.get("/progress/<job_id>")
def progress(job_id):
    job = job_snapshot(job_id)
    if not job:
        return jsonify(success=False, error="Job not found."), 404
    if job["status"] == "completed":
        job["download_url"] = f"/get-file/{job['filename']}"
        job["cleanup_url"] = f"/cleanup/{job['filename']}"
    return jsonify(success=True, **job)


@app.delete("/cleanup/<path:filename>")
def cleanup_download(filename):
    requested = Path(filename)
    if requested.name != filename or requested.name.startswith("."):
        return jsonify(success=False, error="Invalid filename."), 400
    deleted = cleanup_file(DOWNLOAD_FOLDER / requested.name)
    return jsonify(success=True, deleted=deleted)


@app.get("/get-file/<path:filename>")
def get_file(filename):
    requested = Path(filename)
    if requested.name != filename or requested.name.startswith("."):
        return jsonify(success=False, error="Invalid filename."), 400
    file_path = (DOWNLOAD_FOLDER / requested.name).resolve()
    if file_path.parent != DOWNLOAD_FOLDER.resolve() or not file_path.is_file():
        return jsonify(success=False, error="File not found or expired."), 404

    def stream_file():
        try:
            with file_path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk
        finally:
            cleanup_file(file_path)

    response = Response(stream_file(), mimetype="application/octet-stream")
    response.headers["Content-Disposition"] = f'attachment; filename="{requested.name}"'
    response.call_on_close(lambda: cleanup_file(file_path))
    return response


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify(success=False, error="Request is too large."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "80")), debug=False)
