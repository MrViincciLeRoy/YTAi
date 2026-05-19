#!/usr/bin/env python3
import os
import re
import json
import sys
import time
import socket
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OUTPUT_DIR     = Path("shorts_output")
VIDEOS_DIR     = Path("downloaded_videos")
MAX_VIDEOS     = 3
MAX_CLIPS      = 5
SHORT_MIN_SEC  = 45
SHORT_MAX_SEC  = 170
RETRY_ATTEMPTS = 3
RETRY_DELAY    = 5
TOR_PROXY      = "socks5h://127.0.0.1:9050"

INNERTUBE_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8"

# Clients tried in order for player requests — first with plain url fields wins
PLAYER_CLIENTS = [
    {"clientName": "ANDROID_EMBEDDED_PLAYER", "clientVersion": "20.10.38",
     "androidSdkVersion": 30,
     "userAgent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip",
     "hl": "en", "gl": "US"},
    {"clientName": "ANDROID", "clientVersion": "20.10.38",
     "androidSdkVersion": 30,
     "userAgent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip",
     "hl": "en", "gl": "US"},
    {"clientName": "TVHTML5", "clientVersion": "7.20241201.13.00",
     "hl": "en", "gl": "US"},
    {"clientName": "IOS", "clientVersion": "20.10.3",
     "hl": "en", "gl": "US"},
]
WEB_CONTEXT = {
    "client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00",
               "hl": "en", "gl": "US"}
}
HEADERS = {
    "Content-Type":   "application/json",
    "User-Agent":     "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip",
    "X-Goog-Api-Key": INNERTUBE_KEY,
}

# ── Tor ────────────────────────────────────────────────────────────────────────

def tor_is_running() -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect(("127.0.0.1", 9050))
        return True
    except Exception:
        return False
    finally:
        s.close()


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    if tor_is_running():
        session.proxies.update({"http": TOR_PROXY, "https": TOR_PROXY})
        print("Routing through Tor")
    else:
        print("Tor not running - direct connection")
    return session


SESSION = None


# ── Innertube helpers ──────────────────────────────────────────────────────────

def innertube_post(endpoint: str, payload: dict, context: dict = None) -> dict:
    url  = f"https://www.youtube.com/youtubei/v1/{endpoint}?key={INNERTUBE_KEY}"
    ctx  = context if context is not None else WEB_CONTEXT
    body = {"context": ctx, **payload}
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            r = SESSION.post(url, json=body, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY * attempt)
            else:
                raise RuntimeError(f"Innertube {endpoint} failed: {e}")


# ── Channel scraping ───────────────────────────────────────────────────────────

def resolve_browse_id(channel_input: str) -> str:
    if re.match(r'^UC[\w\-]{22}$', channel_input):
        return channel_input

    handle = channel_input.lstrip("@")

    # Try scraping the channel page — multiple patterns since YouTube's HTML varies
    url = f"https://www.youtube.com/@{handle}"
    try:
        r = SESSION.get(url, timeout=20, headers={
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        r.raise_for_status()
        html = r.text
        patterns = [
            r'"browseId"\s*:\s*"(UC[\w\-]{22})"',
            r'"channelId"\s*:\s*"(UC[\w\-]{22})"',
            r'"externalId"\s*:\s*"(UC[\w\-]{22})"',
            r'youtube\.com/channel/(UC[\w\-]{22})',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"  HTML scrape failed: {e}")

    # Fallback: Innertube search for the channel handle
    print(f"  HTML scrape found no browseId — trying Innertube search...")
    data = innertube_post("search", {"query": handle})
    def walk_search(obj):
        if isinstance(obj, dict):
            if obj.get("channelId", "").startswith("UC"):
                return obj["channelId"]
            if obj.get("browseId", "").startswith("UC"):
                return obj["browseId"]
            for v in obj.values():
                res = walk_search(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for v in obj:
                res = walk_search(v)
                if res:
                    return res
        return None

    found = walk_search(data)
    if found:
        return found

    raise RuntimeError(f"Could not resolve browseId for {channel_input}")


def get_viral_videos(channel_input: str) -> list:
    print(f"\nScanning channel: {channel_input}")
    browse_id = resolve_browse_id(channel_input)
    print(f"browseId: {browse_id}")

    data = innertube_post("browse", {
        "browseId": browse_id,
        "params":   "EgZ2aWRlb3PyBgQKAjoA",
    })

    raw_items = []
    def walk(obj):
        if isinstance(obj, dict):
            if "videoRenderer" in obj:
                raw_items.append(obj["videoRenderer"])
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(data)

    videos = []
    for item in raw_items:
        vid_id     = item.get("videoId", "")
        title      = (item.get("title", {}).get("runs", [{}])[0].get("text", ""))
        view_text  = (item.get("viewCountText", {}).get("simpleText", "0"))
        view_count = int(re.sub(r"[^\d]", "", view_text) or "0")
        dur_label  = (item.get("lengthText", {}).get("simpleText", "0:00"))
        parts      = dur_label.split(":")
        if len(parts) == 3:
            duration = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            duration = int(parts[0]) * 60 + int(parts[1])
        else:
            duration = 0

        if not vid_id or not title:
            continue

        print(f"  {title[:55]} | views={view_count:,} | dur={duration}s")

        if duration > 480:
            videos.append({
                "id":       vid_id,
                "title":    title,
                "views":    view_count,
                "duration": duration,
                "url":      f"https://www.youtube.com/watch?v={vid_id}",
            })

    if not videos:
        print("No long-form videos found.")
        sys.exit(1)

    videos.sort(key=lambda v: v["views"], reverse=True)
    top = videos[:MAX_VIDEOS]
    print(f"\nTop {len(top)} videos:")
    for v in top:
        print(f"  {v['title'][:60]} - {v['views']:,} views - {v['duration']//60}m")
    return top


# ── Player / stream ────────────────────────────────────────────────────────────

def get_player_data(video_id: str) -> tuple:
    """
    Try each client in PLAYER_CLIENTS until we get plain url fields.
    Returns (player_data, client_context) so the caller knows which client worked.
    """
    for client in PLAYER_CLIENTS:
        ctx  = {"client": client}
        data = innertube_post("player", {"videoId": video_id}, context=ctx)
        status = data.get("playabilityStatus", {}).get("status", "UNKNOWN")
        fmts   = data.get("streamingData", {}).get("formats", [])
        adap   = data.get("streamingData", {}).get("adaptiveFormats", [])
        all_f  = fmts + adap
        has_plain_url = any(f.get("url") for f in all_f)
        print(f"  client={client['clientName']} status={status} fmts={len(fmts)} adap={len(adap)} plain_url={has_plain_url}")
        if has_plain_url:
            return data
    # Return last attempt even if no plain urls (caller will handle)
    return data


def best_stream_url(player_data: dict):
    """
    Prefer progressive formats (video+audio). Fall back to best video-only mp4.
    Only return formats with a plain url — skip signatureCipher ones.
    """
    sd      = player_data.get("streamingData", {})
    fmts    = [f for f in sd.get("formats", [])          if f.get("url")]
    adap_v  = [f for f in sd.get("adaptiveFormats", [])  if f.get("url") and "video" in f.get("mimeType", "")]

    if fmts:
        fmts.sort(key=lambda f: min(f.get("height", 0), 1080), reverse=True)
        return fmts[0]["url"]

    if adap_v:
        adap_v.sort(key=lambda f: min(f.get("height", 0), 1080), reverse=True)
        return adap_v[0]["url"]

    return None


# ── Download ───────────────────────────────────────────────────────────────────

def download_video(video: dict) -> tuple:
    vid_id     = video["id"]
    safe_name  = re.sub(r"[^\w\-]", "_", video["title"])[:50]
    base_path  = VIDEOS_DIR / f"{vid_id}_{safe_name}"
    video_path = base_path.with_suffix(".mp4")

    print(f"\nDownloading: {video['title'][:60]}")

    player_data = None

    if not video_path.exists():
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                player_data = get_player_data(vid_id)
                stream_url  = best_stream_url(player_data)
                if not stream_url:
                    raise RuntimeError("No usable stream URL")

                # Use a direct session for CDN download — googlevideo.com blocks Tor exit IPs
                dl_session = requests.Session()
                dl_session.headers.update({"User-Agent": "com.google.android.youtube/20.10.38 (Linux; U; Android 11) gzip"})

                r = dl_session.get(stream_url, stream=True, timeout=60)
                r.raise_for_status()

                total    = int(r.headers.get("content-length", 0))
                received = 0
                with open(video_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=262144):
                        if chunk:
                            f.write(chunk)
                            received += len(chunk)
                            if total:
                                print(f"\r  {received/total*100:.1f}%", end="", flush=True)
                print()
                print(f"  Saved {video_path.stat().st_size / 1e6:.1f} MB")
                break

            except Exception as e:
                if attempt < RETRY_ATTEMPTS:
                    wait = RETRY_DELAY * attempt
                    print(f"  Retry {attempt} in {wait}s: {e}")
                    time.sleep(wait)
                    player_data = None
                else:
                    print(f"  Download failed: {e}")
                    return None, None
    else:
        print("  Already downloaded.")
        player_data = get_player_data(vid_id)

    transcript = get_transcript(vid_id, player_data)
    return video_path, transcript


# ── Transcript ─────────────────────────────────────────────────────────────────

def get_transcript(video_id: str, player_data: dict = None) -> str:
    print(f"\nFetching transcript...")

    if player_data is None:
        player_data = get_player_data(video_id)

    tracks = (player_data
              .get("captions", {})
              .get("playerCaptionsTracklistRenderer", {})
              .get("captionTracks", []))

    if not tracks:
        print("  No caption tracks.")
        return None

    track = (
        next((t for t in tracks if t.get("languageCode") == "en" and "asr" not in t.get("kind", "")), None)
        or next((t for t in tracks if t.get("languageCode") == "en"), None)
        or tracks[0]
    )

    base_url = track.get("baseUrl", "")
    if not base_url:
        return None

    url = re.sub(r"&fmt=[^&]+", "", base_url)

    try:
        r = SESSION.get(url, timeout=20)
        r.raise_for_status()
        return parse_caption_xml(r.text)
    except Exception as e:
        print(f"  Transcript fetch failed: {e}")
        return None


def parse_caption_xml(xml_text: str) -> str:
    lines = []
    try:
        root = ET.fromstring(xml_text)
        for el in root.iter("text"):
            start = float(el.get("start", 0))
            raw   = el.text or ""
            clean = re.sub(r"<[^>]+>", "", raw).strip()
            clean = (clean.replace("&amp;", "&").replace("&lt;", "<")
                         .replace("&gt;", ">").replace("&#39;", "'"))
            if clean:
                h, m, s = int(start//3600), int((start%3600)//60), int(start%60)
                lines.append(f"{h:02d}:{m:02d}:{s:02d} {clean}")
    except ET.ParseError as e:
        print(f"  XML parse error: {e}")
    return "\n".join(lines)


# ── Gemini (pure urllib) ───────────────────────────────────────────────────────

def ai_find_clips(transcript: str, video_title: str) -> list:
    print("\nAsking Gemini for clip suggestions...")

    if len(transcript) > 80000:
        transcript = transcript[:80000]

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

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4}
    }).encode("utf-8")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw   = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        raw   = re.sub(r"^```(?:json)?\s*", "", raw)
        raw   = re.sub(r"\s*```$", "", raw)
        clips = json.loads(raw)
        print(f"  Gemini found {len(clips)} clips")
        return clips
    except Exception as e:
        print(f"  Gemini error: {e}")
        return []


# ── ffmpeg ─────────────────────────────────────────────────────────────────────

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


def cut_clips(video_path: Path, clips: list, video_title: str) -> int:
    import subprocess
    print(f"\nCutting {len(clips)} shorts...")

    safe_title = re.sub(r"[^\w\-]", "_", video_title)[:30]
    out_dir    = OUTPUT_DIR / safe_title
    out_dir.mkdir(exist_ok=True)

    cut_count = 0
    for i, clip in enumerate(clips, 1):
        start_sec = ts_to_seconds(clip.get("start", "0:00:00"))
        end_sec   = ts_to_seconds(clip.get("end",   "0:00:00"))
        duration  = end_sec - start_sec

        if duration < 10:
            print(f"  Clip {i} too short, skipping.")
            continue

        safe_clip = re.sub(r"[^\w\-]", "_", clip.get("title", f"clip_{i}"))[:50]
        out_path  = out_dir / f"{i:02d}_{safe_clip}.mp4"

        print(f"\n  [{i}/{len(clips)}] {clip.get('title', 'Untitled')}")
        print(f"         {clip.get('start')} -> {clip.get('end')} ({duration:.0f}s)")

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
            print(f"         OK {out_path.name} ({size_mb:.1f} MB)")
            cut_count += 1
            out_path.with_suffix(".json").write_text(json.dumps({
                "title":    clip.get("title"),
                "hook":     clip.get("hook"),
                "why":      clip.get("why"),
                "start":    clip.get("start"),
                "end":      clip.get("end"),
                "source":   video_title,
                "duration": duration,
            }, indent=2))
        else:
            print(f"         FAIL ffmpeg: {result.stderr[:150]}")
    return cut_count


# ── Main ───────────────────────────────────────────────────────────────────────

def run(channel_input: str):
    global SESSION

    OUTPUT_DIR.mkdir(exist_ok=True)
    VIDEOS_DIR.mkdir(exist_ok=True)

    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not set.")
        sys.exit(1)

    SESSION = make_session()

    print("\n" + "=" * 55)
    print("  SHORTS CLIPPER  - Pure Requests")
    print("=" * 55)

    videos      = get_viral_videos(channel_input)
    total_clips = 0

    for video in videos:
        print("\n" + "-" * 55)
        print(f"Processing: {video['title'][:60]}")
        print("-" * 55)

        video_path, transcript = download_video(video)
        if not video_path:
            print("  Skipping - download failed.")
            continue
        if not transcript:
            print("  Skipping - no transcript.")
            continue

        clips = ai_find_clips(transcript, video["title"])
        if not clips:
            print("  Skipping - no clips found.")
            continue

        total_clips += cut_clips(video_path, clips, video["title"])

    print("\n" + "=" * 55)
    print(f"  Done! {total_clips} shorts -> {OUTPUT_DIR}/")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage: python shorts_clipper.py @freecodecamp\n")
        sys.exit(0)
    run(sys.argv[1])