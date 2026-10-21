"""
Snap Downloader Tool — Snapchat video downloader WITHOUT watermark (website)

Pipeline:
  1. User Snapchat Spotlight / share link deta hai
  2. Server page se direct CDN video file nikaalta hai
  3. Watermark removal: Snapchat ka watermark video ke ANDAR jala hota hai
     (animated — pehle top-left, phir bottom-right "Snapchat @username").
     Blur NAHI kiya jata: watermark wali jagah video ke apne un frames se
     restore ki jati hai jahan wo jagah saaf hoti hai (background transplant),
     is liye background clear/sharp rehta hai.
  4. Saaf video download ke liye milti hai.

Run:  ./venv/bin/python app.py   ->  http://localhost:5000
Needs: ffmpeg + ffprobe (system par honay chahiye)
"""

import html
import json
import os
import re
import subprocess
import tempfile
import uuid

import requests
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SNAP_HOSTS = ("snapchat.com", "www.snapchat.com", "t.snapchat.com")
WORK_DIR = os.path.join(tempfile.gettempdir(), "snap-tool")
os.makedirs(WORK_DIR, exist_ok=True)


# ---------------------------------------------------------------- extraction
def is_snapchat_url(url: str) -> bool:
    return any(h in url for h in SNAP_HOSTS)


def extract_video_url(page_html: str) -> str | None:
    for pattern in (
        r'<meta[^>]+property=["\']og:video(?::url|:secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:video',
        r'<meta[^>]+name=["\']twitter:player:stream["\'][^>]+content=["\']([^"\']+)["\']',
    ):
        m = re.search(pattern, page_html)
        if m:
            return html.unescape(m.group(1))
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page_html, re.DOTALL)
    if m:
        try:
            found = _find_mp4(json.loads(m.group(1)))
            if found:
                return found
        except (json.JSONDecodeError, TypeError):
            pass
    m = re.search(r'https://[^"\']+?\.mp4[^"\']*', page_html)
    if m and ("sc-cdn" in m.group(0) or "snapchat" in m.group(0)):
        return html.unescape(m.group(0)).replace("\\u0026", "&")
    return None


def _find_mp4(obj, depth=0):
    if depth > 12 or obj is None:
        return None
    if isinstance(obj, str):
        return obj if obj.startswith("http") and ".mp4" in obj else None
    if isinstance(obj, dict):
        for key in ("videoUrl", "mediaUrl", "video_url", "url"):
            val = obj.get(key)
            if isinstance(val, str) and ".mp4" in val:
                return val
        for val in obj.values():
            found = _find_mp4(val, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_mp4(item, depth + 1)
            if found:
                return found
    return None


def resolve_video(snap_url: str) -> tuple[str, str | None]:
    """Snapchat link -> (direct video URL, thumbnail URL|None)."""
    resp = requests.get(snap_url, headers=HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    video_url = extract_video_url(resp.text)
    if not video_url:
        raise ValueError("no_video")
    thumb = None
    m = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        resp.text,
    )
    if m:
        thumb = html.unescape(m.group(1))
    return video_url, thumb


# ------------------------------------------------------- watermark removal
def probe(path: str) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-show_entries", "format=duration",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    stream = data["streams"][0]
    return stream["width"], stream["height"], float(data["format"]["duration"])


def grab_frame(video: str, t: float, out_png: str):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video,
         "-frames:v", "1", out_png],
        check=True,
    )


def remove_watermark(src: str, dst: str, job_dir: str) -> None:
    """Snapchat ke standard animated watermark ko background-transplant se
    hatana. Positions video ke size ke hisab se relative hain:
      - Top-left stack (ghost + Snapchat + @user): video ke pehle ~50% mein
      - Bottom-right stack: ~40% ke baad se aakhir tak
    Reference frames wohi video se liye jate hain (BR ke liye shuru ka frame
    jahan BR saaf hai; TL ke liye aakhir ka frame jahan TL saaf hai)."""
    w, h, dur = probe(src)
    ref_br = os.path.join(job_dir, "ref_br.png")   # BR saaf: shuru ka frame
    ref_tl = os.path.join(job_dir, "ref_tl.png")   # TL saaf: aakhir ka frame
    grab_frame(src, dur * 0.10, ref_br)
    grab_frame(src, dur * 0.80, ref_tl)

    # Bottom-right box
    bx, by = round(w * 0.570), round(h * 0.775)
    bw, bh = w - bx, h - by
    # Top-left box
    tx, ty = 0, round(h * 0.040)
    tw, th = round(w * 0.315), round(h * 0.224)

    filt = (
        f"[1:v]crop={bw}:{bh}:{bx}:{by},format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='clip(min(min(X,Y),10)*255/10,0,255)'[brp];"
        f"[2:v]crop={tw}:{th}:{tx}:{ty},format=rgba,"
        f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        f"a='clip(min(min({tw - 1}-X,min(Y,{th - 1}-Y)),12)*255/12,0,255)'[tlp];"
        f"[0:v][brp]overlay={bx}:{by}:enable='gte(t,{dur * 0.40:.2f})'[s1];"
        f"[s1][tlp]overlay={tx}:{ty}:enable='lte(t,{dur * 0.50:.2f})'[v]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-i", ref_br, "-i", ref_tl,
         "-filter_complex", filt, "-map", "[v]", "-map", "0:a?",
         "-c:a", "copy", "-movflags", "+faststart", dst],
        check=True,
    )


# ------------------------------------------------------------------- routes
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/fetch", methods=["POST"])
def fetch_video():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url.startswith("http") or not is_snapchat_url(url):
        return jsonify(error="Please paste a valid Snapchat link."), 400
    try:
        video_url, thumb = resolve_video(url)
    except requests.RequestException:
        return jsonify(error="Snapchat se page load nahi ho saka. Link check karein."), 502
    except ValueError:
        return jsonify(error="Is link mein video nahi mili. Public Spotlight/Snap link paste karein."), 404
    return jsonify(video=video_url, thumbnail=thumb)


@app.route("/api/download-clean", methods=["POST"])
def download_clean():
    """Link -> video download -> watermark remove -> saaf file download."""
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url.startswith("http") or not is_snapchat_url(url):
        return jsonify(error="Please paste a valid Snapchat link."), 400
    try:
        video_url, _ = resolve_video(url)
    except (requests.RequestException, ValueError):
        return jsonify(error="Video nahi mil saki. Link check karein."), 502

    job_dir = os.path.join(WORK_DIR, uuid.uuid4().hex[:10])
    os.makedirs(job_dir, exist_ok=True)
    raw = os.path.join(job_dir, "raw.mp4")
    clean = os.path.join(job_dir, "snapchat-no-watermark.mp4")
    try:
        with requests.get(video_url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(raw, "wb") as f:
                for chunk in r.iter_content(64 * 1024):
                    f.write(chunk)
        remove_watermark(raw, clean, job_dir)
        return send_file(clean, as_attachment=True,
                         download_name="snapchat-no-watermark.mp4")
    except Exception:
        # Processing fail ho jaye to kam az kam original video de do
        if os.path.exists(raw) and os.path.getsize(raw) > 0:
            return send_file(raw, as_attachment=True,
                             download_name="snapchat-video.mp4")
        return jsonify(error="Download fail ho gaya. Dobara try karein."), 500


@app.route("/download")
def download():
    """Original CDN video ka seedha download (bina processing)."""
    video_url = request.args.get("url", "")
    if not video_url.startswith("https://") or (
        "sc-cdn.net" not in video_url and "snapchat" not in video_url
    ):
        return "Invalid video URL", 400
    upstream = requests.get(video_url, headers=HEADERS, stream=True, timeout=30)
    upstream.raise_for_status()

    def generate():
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        content_type=upstream.headers.get("Content-Type", "video/mp4"),
        headers={
            "Content-Disposition": 'attachment; filename="snapchat-video.mp4"',
            "Content-Length": upstream.headers.get("Content-Length", ""),
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
