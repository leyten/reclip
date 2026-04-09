import os
import re
import uuid
import glob
import json
import time
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
from flask import Flask, request, jsonify, send_file, render_template, abort

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
LIBRARY_DIR = os.path.join(BASE_DIR, "library")
LIBRARY_INDEX = os.path.join(LIBRARY_DIR, "index.json")

# YouTube needs cookies (datacenter IPs hit the bot wall) plus a JS runtime
# and the remote EJS challenge solver to defeat YouTube's "n parameter".
YT_COOKIES = "/etc/reclip/youtube-cookies.txt"
YT_CACHE = "/tmp/yt-dlp-cache"
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(h in u for h in YOUTUBE_HOSTS)


def ytdlp_extra_args(url: str):
    """Return extra yt-dlp CLI args for sites that need them.
    Only enables remote-component fetching for YouTube — limits the blast
    radius of running github-hosted JS to the one extractor that needs it.
    """
    args = ["--cache-dir", YT_CACHE]
    if is_youtube(url) and os.path.exists(YT_COOKIES):
        args += [
            "--cookies", YT_COOKIES,
            "--js-runtimes", "node",
            "--remote-components", "ejs:github",
            # Ask the YouTube API in English so the API returns English-
            # first metadata and audio ordering. The cookied burner account's
            # account language can otherwise leak through (e.g. Spanish).
            "--extractor-args", "youtube:lang=en",
        ]
    return args


def audio_selector(url: str) -> str:
    """For sites that publish multi-language audio dubs (notably YouTube),
    prefer English audio when present, fall back to best audio otherwise."""
    if is_youtube(url):
        return "bestaudio[language^=en]/bestaudio"
    return "bestaudio"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(LIBRARY_DIR, exist_ok=True)

jobs = {}
library_lock = threading.Lock()


# ---------- helpers ----------

def sanitize_title(title, max_len=60):
    if not title:
        return ""
    cleaned = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    return cleaned[:max_len].strip()


def load_library():
    if not os.path.exists(LIBRARY_INDEX):
        return []
    try:
        with open(LIBRARY_INDEX, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def write_library(items):
    tmp = LIBRARY_INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, LIBRARY_INDEX)


def fetch_thumbnail(url, dest_path):
    """Download a thumbnail URL to dest_path. Returns True on success."""
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ReClip/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(5 * 1024 * 1024)  # 5MB cap
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def ext_to_mimetype(ext):
    return {
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
    }.get(ext.lower(), "application/octet-stream")


# ---------- yt-dlp download worker ----------

def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template, *ytdlp_extra_args(url)]
    audio_sel = audio_selector(url)

    if format_choice == "audio":
        cmd += ["-f", audio_sel, "-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+{audio_sel}/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", f"bestvideo+{audio_sel}/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        safe_title = sanitize_title(title, 20)
        job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


# ---------- routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", *ytdlp_extra_args(url), url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Filter to DASH video-only formats (vcodec set, acodec='none').
        # We deliberately skip HLS combined formats (e.g. 94-15, 301-7 on
        # YouTube) because those bake in a specific audio language at
        # extraction time, defeating any download-time language selector.
        # By picking video-only formats, the audio_selector() language
        # filter actually works.
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if height and vcodec != "none" and acodec == "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    thumbnail = data.get("thumbnail", "")
    uploader = data.get("uploader", "")
    duration = data.get("duration")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "thumbnail": thumbnail,
        "uploader": uploader,
        "duration": duration,
        "format_choice": format_choice,
    }

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


# ---------- library endpoints ----------

@app.route("/api/library/save/<job_id>", methods=["POST"])
def library_save(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "Job not ready"}), 404

    src = job.get("file")
    if not src or not os.path.exists(src):
        return jsonify({"error": "Source file missing"}), 404

    ext = os.path.splitext(src)[1]
    lib_id = uuid.uuid4().hex[:12]
    dest = os.path.join(LIBRARY_DIR, f"{lib_id}{ext}")

    try:
        shutil.move(src, dest)
    except OSError as e:
        return jsonify({"error": f"Could not move file: {e}"}), 500

    has_thumb = False
    thumb_url = job.get("thumbnail", "")
    if thumb_url:
        thumb_dest = os.path.join(LIBRARY_DIR, f"{lib_id}.jpg")
        has_thumb = fetch_thumbnail(thumb_url, thumb_dest)

    title = job.get("title", "") or "Untitled"
    safe_title = sanitize_title(title, 80) or "video"
    display_filename = f"{safe_title}{ext}"

    entry = {
        "id": lib_id,
        "title": title,
        "filename": display_filename,
        "url": job.get("url", ""),
        "format": job.get("format_choice", "video"),
        "ext": ext.lstrip("."),
        "saved_at": int(time.time()),
        "size": os.path.getsize(dest),
        "uploader": job.get("uploader", ""),
        "duration": job.get("duration"),
        "has_thumb": has_thumb,
    }

    with library_lock:
        items = load_library()
        items.append(entry)
        write_library(items)

    # Clean up the in-memory job — file is gone
    job["status"] = "saved"
    job["file"] = None

    return jsonify({"ok": True, "entry": entry})


@app.route("/api/library", methods=["GET"])
def library_list():
    with library_lock:
        items = load_library()
    items_sorted = sorted(items, key=lambda x: x.get("saved_at", 0), reverse=True)
    total_size = sum(i.get("size", 0) for i in items)
    return jsonify({"items": items_sorted, "total_size": total_size, "count": len(items)})


def _find_library_entry(lib_id):
    items = load_library()
    for it in items:
        if it.get("id") == lib_id:
            return it
    return None


def _library_file_path(entry):
    return os.path.join(LIBRARY_DIR, f"{entry['id']}.{entry['ext']}")


@app.route("/api/library/<lib_id>/file")
def library_play(lib_id):
    """Inline stream for in-browser playback (supports Range requests)."""
    entry = _find_library_entry(lib_id)
    if not entry:
        abort(404)
    path = _library_file_path(entry)
    if not os.path.exists(path):
        abort(404)
    mimetype = ext_to_mimetype(os.path.splitext(path)[1])
    return send_file(path, mimetype=mimetype, conditional=True)


@app.route("/api/library/<lib_id>/download")
def library_download(lib_id):
    """Force-download as attachment."""
    entry = _find_library_entry(lib_id)
    if not entry:
        abort(404)
    path = _library_file_path(entry)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=entry.get("filename") or os.path.basename(path))


@app.route("/api/library/<lib_id>/thumb")
def library_thumb(lib_id):
    entry = _find_library_entry(lib_id)
    if not entry or not entry.get("has_thumb"):
        abort(404)
    path = os.path.join(LIBRARY_DIR, f"{lib_id}.jpg")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", conditional=True)


@app.route("/api/library/<lib_id>", methods=["DELETE"])
def library_delete(lib_id):
    with library_lock:
        items = load_library()
        entry = next((it for it in items if it.get("id") == lib_id), None)
        if not entry:
            return jsonify({"error": "Not found"}), 404

        # Remove file + thumb
        for p in (
            _library_file_path(entry),
            os.path.join(LIBRARY_DIR, f"{lib_id}.jpg"),
        ):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

        items = [it for it in items if it.get("id") != lib_id]
        write_library(items)

    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
