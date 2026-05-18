#!/usr/bin/env python3
"""
Shorts Clipper - Automated YouTube Shorts Generator
Pipeline: Channel → Viral Videos (YouTube Data API) → Download (iOS client) → AI Clip Detection → ffmpeg Cut
"""

import os
import re
import json
import subprocess
import sys
import time
from pathlib import Path
import urllib.request
import urllib.parse

# ── CONFIG ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
OUTPUT_DIR      = Path("shorts_output")
VIDEOS_DIR      = Path("downloaded_videos")
MAX_VIDEOS      = 3
MAX_CLIPS       = 5
SHORT_MIN_SEC   = 45
SHORT_MAX_SEC   = 170
RETRY_ATTEMPTS  = 3
RETRY_DELAY     = 5
COOKIES_FILE    = "cookies.txt"
# ──────────────────────────────────────────────────────────────────────────

def cookies_args() -> list:
    if Path(COOKIES_FILE).exists() and Path(COOKIES_FILE).stat().st_size > 0:
        return ["--cookies", COOKIES_FILE]
    return []

def setup():
    OUTPUT_DIR.mkdir(exist_ok=True)
    VIDEOS_DIR.mkdir(exist_ok=True)

def log(msg, emoji="▶"):
    print(f"\n{emoji}  {msg}")

def yt_api(endpoint: str, params: dict) -> dict:
    params["key"] = YOUTUBE_API_KEY
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())

def iso8601_to_seconds(duration: str) -> int:
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration)
    if not m:
        return 0
    h, mn, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mn * 60 + s

def get_channel_id(handle: str) -> str:
    handle = handle.lstrip("@")
    data = yt_api("search", {
        "part": "snippet",
        "q": handle,
        "type": "channel",
        "maxResults": 1
    })
    items = data.get("items", [])
    if not items:
        print(f"Channel not found: {handle}")
        sys.exit(1)
    return items[0]["snippet"]["channelId"]

def get_viral_videos(channel_input: str) -> list[dict]:
    log(f"Scanning channel: {channel_input}", "🔍")

    if channel_input.startswith("UC") and len(channel_input) == 24:
        channel_id = channel_input
    else:
        channel_id = get_channel_id(channel_input.lstrip("@"))

    log(f"Channel ID: {channel_id}", "📋")

    data = yt_api("channels", {
        "part": "contentDetails",
        "id": channel_id
    })
    uploads_playlist = data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    playlist_data = yt_api("playlistItems", {
        "part": "contentDetails",
        "playlistId": uploads_playlist,
        "maxResults": 50
    })
    video_ids = [item["contentDetails"]["videoId"] for item in playlist_data.get("items", [])]

    if not video_ids:
        print("No videos found.")
        sys.exit(1)

    details = yt_api("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(video_ids)
    })

    videos = []
    for item in details.get("items", []):
        dur_sec    = iso8601_to_seconds(item["contentDetails"]["duration"])
        view_count = int(item["statistics"].get("viewCount", 0))
        vid_id     = item["id"]
        title      = item["snippet"]["title"]

        print(f"  {title[:50]} | views={view_count:,} | dur={dur_sec}s")

        if dur_sec > 480:
            videos.append({
                "id":       vid_id,
                "title":    title,
                "views":    view_count,
                "duration": dur_sec,
                "url":      f"https://www.youtube.com/watch?v={vid_id}"
            })

    if not videos:
        print("No long-form videos (>8 min) found.")
        sys.exit(1)

    videos.sort(key=lambda v: v["views"], reverse=True)
    top = videos[:MAX_VIDEOS]

    log(f"Top {len(top)} viral videos:", "📊")
    for v in top:
        print(f"   • {v['title'][:60]} — {v['views']:,} views — {v['duration']//60}m")

    return top


def download_video_and_transcript(video: dict) -> tuple[Path | None, str | None]:
    vid_id    = video["id"]
    safe_name = re.sub(r'[^\w\-]', '_', video["title"])[:50]
    base_path = VIDEOS_DIR / f"{vid_id}_{safe_name}"

    log(f"Downloading: {video['title'][:60]}", "⬇️")

    video_path = base_path.with_suffix(".mp4")
    if not video_path.exists():
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            cmd = [
                "yt-dlp",
                *cookies_args(),
                "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "-o", str(video_path),
                "--no-warnings",
                "--socket-timeout", "30",
                video["url"]
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                break
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_DELAY * (2 ** (attempt - 1))
                print(f"  ⏳ Retrying download in {wait}s... stderr: {result.stderr[:100]}")
                time.sleep(wait)
            else:
                print(f"  Download failed: {result.stderr[:300]}")
                return None, None
    else:
        print("  Already downloaded, skipping.")

    transcript_path = base_path.with_suffix(".txt")
    if not transcript_path.exists():
        log("Fetching transcript...", "📝")
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            cmd = [
                "yt-dlp",
                *cookies_args(),
                "--write-auto-subs",
                "--sub-format", "vtt",
                "--sub-lang", "en",
                "--skip-download",
                "--no-warnings",
                "--socket-timeout", "30",
                "-o", str(base_path),
                video["url"]
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                break
            if attempt < RETRY_ATTEMPTS:
                wait = RETRY_DELAY * (2 ** (attempt - 1))
                print(f"  ⏳ Retrying transcript in {wait}s...")
                time.sleep(wait)

        vtt_files = list(VIDEOS_DIR.glob(f"{vid_id}*.vtt"))
        if vtt_files:
            raw = vtt_files[0].read_text(encoding="utf-8", errors="ignore")
            transcript = vtt_to_plain(raw)
            transcript_path.write_text(transcript)
            vtt_files[0].unlink()
        else:
            print("  No transcript available.")
            return video_path, None
    else:
        transcript = transcript_path.read_text()
        return video_path, transcript

    transcript = transcript_path.read_text() if transcript_path.exists() else None
    return video_path, transcript


def vtt_to_plain(vtt_content: str) -> str:
    lines = vtt_content.split("\n")
    result = []
    timestamp_re = re.compile(r'(\d{2}:\d{2}:\d{2})\.\d+ --> ')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = timestamp_re.match(line)
        if m:
            ts = m.group(1)
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip() and "-->" not in lines[i]:
                clean = re.sub(r'<[^>]+>', '', lines[i].strip())
                if clean:
                    text_parts.append(clean)
                i += 1
            if text_parts:
                result.append(f"{ts} {' '.join(text_parts)}")
        else:
            i += 1
    return "\n".join(result)


def ai_find_clips(transcript: str, video_title: str) -> list[dict]:
    log("Asking Gemini to find best clips...", "🤖")
    try:
        import google.generativeai as genai
    except ImportError:
        print("Run: pip install google-generativeai")
        sys.exit(1)

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    max_chars = 80000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars]
        print(f"  Transcript trimmed to {max_chars} chars.")

    prompt = f"""You are an expert YouTube Shorts editor for a tech/programming channel.

Analyze this transcript from: "{video_title}"

Find exactly {MAX_CLIPS} moments that make great standalone YouTube Shorts for developers.

RULES:
- Each clip must be {SHORT_MIN_SEC}-{SHORT_MAX_SEC} seconds long
- Each clip must be self-contained
- Prioritize: surprising facts, "aha" moments, practical tips, impressive demos, strong opinions
- Avoid: intros, outros, setup steps

Respond ONLY with a JSON array, no other text:
[
  {{
    "title": "Short punchy title under 60 chars",
    "hook": "First sentence to grab attention",
    "start": "HH:MM:SS",
    "end": "HH:MM:SS",
    "why": "One sentence on why this works as a short"
  }}
]

TRANSCRIPT:
{transcript}"""

    try:
        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        clips = json.loads(raw)
        log(f"Gemini found {len(clips)} clips", "✅")
        return clips
    except json.JSONDecodeError as e:
        print(f"  Invalid JSON from Gemini: {e}")
        print(f"  Raw: {response.text[:300]}")
        return []
    except Exception as e:
        print(f"  Gemini error: {e}")
        return []


def ts_to_seconds(ts: str) -> float:
    parts = ts.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return 0.0


def cut_clips(video_path: Path, clips: list[dict], video_title: str) -> int:
    log(f"Cutting {len(clips)} shorts with ffmpeg...", "✂️")
    safe_title = re.sub(r'[^\w\-]', '_', video_title)[:30]
    out_dir = OUTPUT_DIR / safe_title
    out_dir.mkdir(exist_ok=True)

    cut_count = 0
    for i, clip in enumerate(clips, 1):
        start_sec = ts_to_seconds(clip.get("start", "0:00:00"))
        end_sec   = ts_to_seconds(clip.get("end", "0:00:00"))
        duration  = end_sec - start_sec

        if duration < 10:
            print(f"  Clip {i} too short ({duration:.0f}s), skipping.")
            continue

        safe_clip = re.sub(r'[^\w\-]', '_', clip.get("title", f"clip_{i}"))[:50]
        out_path  = out_dir / f"{i:02d}_{safe_clip}.mp4"

        print(f"\n  [{i}/{len(clips)}] {clip.get('title', 'Untitled')}")
        print(f"         {clip.get('start')} → {clip.get('end')} ({duration:.0f}s)")

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-i", str(video_path),
            "-t", str(duration),
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path), "-loglevel", "error"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"         ✅ {out_path.name} ({size_mb:.1f} MB)")
            cut_count += 1
            out_path.with_suffix(".json").write_text(json.dumps({
                "title": clip.get("title"), "hook": clip.get("hook"),
                "why": clip.get("why"), "start": clip.get("start"),
                "end": clip.get("end"), "source": video_title, "duration": duration
            }, indent=2))
        else:
            print(f"         ❌ ffmpeg: {result.stderr[:150]}")

    return cut_count


def run(channel_input: str):
    setup()

    if not GEMINI_API_KEY:
        print("❌  GEMINI_API_KEY not set.")
        sys.exit(1)
    if not YOUTUBE_API_KEY:
        print("❌  YOUTUBE_API_KEY not set.")
        sys.exit(1)

    print("\n" + "="*55)
    print("  SHORTS CLIPPER  —  Auto Pipeline")
    print("="*55)

    videos = get_viral_videos(channel_input)
    total_clips = 0

    for video in videos:
        print("\n" + "-"*55)
        print(f"Processing: {video['title'][:60]}")
        print("-"*55)

        video_path, transcript = download_video_and_transcript(video)
        if not video_path:
            print("  Skipping — download failed.")
            continue
        if not transcript:
            print("  Skipping — no transcript.")
            continue

        clips = ai_find_clips(transcript, video["title"])
        if not clips:
            print("  Skipping — no clips found.")
            continue

        total_clips += cut_clips(video_path, clips, video["title"])

    print("\n" + "="*55)
    print(f"  Done! {total_clips} shorts → {OUTPUT_DIR}/")
    print("="*55 + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage: python shorts_clipper.py @freecodecamp\n")
        sys.exit(0)
    run(sys.argv[1])
