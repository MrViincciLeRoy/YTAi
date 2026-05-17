#!/usr/bin/env python3
"""
Shorts Clipper - Automated YouTube Shorts Generator
Pipeline: Channel → Viral Videos → Download → AI Clip Detection → ffmpeg Cut
"""

import os
import re
import json
import subprocess
import sys
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")  # Set as GitHub secret or local env var
OUTPUT_DIR     = Path("shorts_output")
VIDEOS_DIR     = Path("downloaded_videos")
MAX_VIDEOS     = 3    # How many viral videos to pull per channel
MAX_CLIPS      = 5    # How many shorts to cut per video
SHORT_MIN_SEC  = 45   # Minimum short length in seconds
SHORT_MAX_SEC  = 170  # Maximum short length in seconds (under 3 min)
# ─────────────────────────────────────────────────────────────────────────────

def setup():
    OUTPUT_DIR.mkdir(exist_ok=True)
    VIDEOS_DIR.mkdir(exist_ok=True)

def log(msg, emoji="▶"):
    print(f"\n{emoji}  {msg}")

def get_viral_videos(channel_input: str) -> list[dict]:
    """Use yt-dlp to fetch the most viewed videos from a channel."""
    log(f"Scanning channel: {channel_input}", "🔍")

    # Accept channel URL or @handle or bare name
    if channel_input.startswith("http"):
        url = channel_input
    elif channel_input.startswith("@"):
        url = f"https://www.youtube.com/{channel_input}/videos"
    else:
        url = f"https://www.youtube.com/@{channel_input}/videos"

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--playlist-end", "30",          # look at last 30 uploads
        "--print", "%(id)s|||%(title)s|||%(view_count)s|||%(duration)s",
        "--no-warnings",
        url
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error fetching channel: {result.stderr[:300]}")
        sys.exit(1)

    videos = []
    for line in result.stdout.strip().split("\n"):
        if "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        vid_id, title, views, duration = parts[0], parts[1], parts[2], parts[3]
        try:
            view_count = int(views) if views and views != "NA" else 0
            dur_sec    = int(duration) if duration and duration != "NA" else 0
        except ValueError:
            continue

        # Only videos longer than 8 minutes (worth clipping)
        if dur_sec > 480:
            videos.append({
                "id":       vid_id,
                "title":    title,
                "views":    view_count,
                "duration": dur_sec,
                "url":      f"https://www.youtube.com/watch?v={vid_id}"
            })

    if not videos:
        print("No long-form videos found on this channel.")
        sys.exit(1)

    # Sort by view count, pick top N
    videos.sort(key=lambda v: v["views"], reverse=True)
    top = videos[:MAX_VIDEOS]

    log(f"Top {len(top)} viral videos found:", "📊")
    for v in top:
        views_fmt = f"{v['views']:,}" if v['views'] else "unknown"
        mins = v['duration'] // 60
        print(f"   • {v['title'][:60]} — {views_fmt} views — {mins}m")

    return top


def download_video_and_transcript(video: dict) -> tuple[Path | None, str | None]:
    """Download video file and auto-transcript via yt-dlp."""
    vid_id    = video["id"]
    safe_name = re.sub(r'[^\w\-]', '_', video["title"])[:50]
    base_path = VIDEOS_DIR / f"{vid_id}_{safe_name}"

    log(f"Downloading: {video['title'][:60]}", "⬇️")

    # Download video (best quality under 1080p to save space)
    video_path = base_path.with_suffix(".mp4")
    if not video_path.exists():
        cmd = [
            "yt-dlp",
            "-f", "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(video_path),
            "--no-warnings",
            video["url"]
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  Video download failed: {result.stderr[:200]}")
            return None, None
    else:
        print(f"  Video already downloaded, skipping.")

    # Download transcript (auto-generated subtitles)
    transcript_path = base_path.with_suffix(".txt")
    if not transcript_path.exists():
        log("Fetching transcript...", "📝")
        cmd = [
            "yt-dlp",
            "--write-auto-subs",
            "--sub-format", "vtt",
            "--sub-lang", "en",
            "--skip-download",
            "--no-warnings",
            "-o", str(base_path),
            video["url"]
        ]
        subprocess.run(cmd, capture_output=True, text=True)

        # Convert .vtt to plain text
        vtt_files = list(VIDEOS_DIR.glob(f"{vid_id}*.vtt"))
        if vtt_files:
            raw = vtt_files[0].read_text(encoding="utf-8", errors="ignore")
            transcript = vtt_to_plain(raw)
            transcript_path.write_text(transcript)
            vtt_files[0].unlink()
        else:
            print("  No transcript available for this video.")
            return video_path, None
    else:
        transcript = transcript_path.read_text()
        return video_path, transcript

    transcript = transcript_path.read_text() if transcript_path.exists() else None
    return video_path, transcript


def vtt_to_plain(vtt_content: str) -> str:
    """Convert VTT subtitle file to plain timestamped text."""
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
    """Send transcript to Gemini and get clip suggestions."""
    log("Asking Gemini to find best clips...", "🤖")

    try:
        import google.generativeai as genai
    except ImportError:
        print("Run: pip install google-generativeai")
        sys.exit(1)

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    # Trim transcript if huge (Gemini free tier has limits)
    max_chars = 80000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars]
        print(f"  Transcript trimmed to {max_chars} chars for API limits.")

    prompt = f"""You are an expert YouTube Shorts editor for a tech/programming channel.

Analyze this transcript from the video: "{video_title}"

Find exactly {MAX_CLIPS} moments that would make great standalone YouTube Shorts for developers.

RULES:
- Each clip must be {SHORT_MIN_SEC}-{SHORT_MAX_SEC} seconds long
- Each clip must be self-contained (viewer doesn't need context)
- Prioritize: surprising facts, "aha" moments, practical tips, impressive demos, strong opinions
- Avoid: intros, outros, setup steps, "as I mentioned earlier" moments

Respond ONLY with a JSON array, no other text. Format:
[
  {{
    "title": "Short punchy title under 60 chars",
    "hook": "First sentence to grab attention (shown as caption overlay)",
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

        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)

        clips = json.loads(raw)
        log(f"Gemini found {len(clips)} clip suggestions", "✅")
        return clips

    except json.JSONDecodeError as e:
        print(f"  Gemini returned invalid JSON: {e}")
        print(f"  Raw response: {response.text[:300]}")
        return []
    except Exception as e:
        print(f"  Gemini API error: {e}")
        return []


def ts_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS or MM:SS to seconds."""
    parts = ts.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0])
    except ValueError:
        return 0.0


def cut_clips(video_path: Path, clips: list[dict], video_title: str):
    """Use ffmpeg to cut each clip from the video."""
    log(f"Cutting {len(clips)} shorts with ffmpeg...", "✂️")

    safe_title = re.sub(r'[^\w\-]', '_', video_title)[:30]
    video_output_dir = OUTPUT_DIR / safe_title
    video_output_dir.mkdir(exist_ok=True)

    cut_count = 0
    for i, clip in enumerate(clips, 1):
        start_sec = ts_to_seconds(clip.get("start", "0:00:00"))
        end_sec   = ts_to_seconds(clip.get("end", "0:00:00"))
        duration  = end_sec - start_sec

        if duration < 10:
            print(f"  Clip {i} too short ({duration:.0f}s), skipping.")
            continue

        safe_clip_title = re.sub(r'[^\w\-]', '_', clip.get("title", f"clip_{i}"))[:50]
        out_path = video_output_dir / f"{i:02d}_{safe_clip_title}.mp4"

        print(f"\n  [{i}/{len(clips)}] {clip.get('title', 'Untitled')}")
        print(f"         {clip.get('start')} → {clip.get('end')} ({duration:.0f}s)")
        print(f"         Hook: {clip.get('hook', '')[:70]}")

        cmd = [
            "ffmpeg",
            "-y",                          # overwrite without asking
            "-ss", str(start_sec),         # seek to start
            "-i", str(video_path),         # input file
            "-t", str(duration),           # duration to cut
            "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",  # vertical 9:16
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(out_path),
            "-loglevel", "error"
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            size_mb = out_path.stat().st_size / (1024 * 1024)
            print(f"         ✅ Saved: {out_path.name} ({size_mb:.1f} MB)")
            cut_count += 1

            # Save metadata alongside the clip
            meta_path = out_path.with_suffix(".json")
            meta_path.write_text(json.dumps({
                "title":    clip.get("title"),
                "hook":     clip.get("hook"),
                "why":      clip.get("why"),
                "start":    clip.get("start"),
                "end":      clip.get("end"),
                "source":   video_title,
                "duration": duration
            }, indent=2))
        else:
            print(f"         ❌ ffmpeg error: {result.stderr[:150]}")

    return cut_count


def run(channel_input: str):
    setup()

    if not GEMINI_API_KEY:
        print("\n❌  GEMINI_API_KEY not set. Locally: export GEMINI_API_KEY=your_key | GitHub: Settings → Secrets → GEMINI_API_KEY")
        print("    Get a free key at: https://aistudio.google.com/app/apikey\n")
        sys.exit(1)

    print("\n" + "="*55)
    print("  SHORTS CLIPPER  —  Auto Pipeline")
    print("="*55)

    # Step 1: Find viral videos
    videos = get_viral_videos(channel_input)

    total_clips = 0

    for video in videos:
        print("\n" + "-"*55)
        print(f"Processing: {video['title'][:60]}")
        print("-"*55)

        # Step 2: Download video + transcript
        video_path, transcript = download_video_and_transcript(video)
        if not video_path:
            print("  Skipping — download failed.")
            continue
        if not transcript:
            print("  Skipping — no transcript available.")
            continue

        # Step 3: AI finds best clips
        clips = ai_find_clips(transcript, video["title"])
        if not clips:
            print("  Skipping — no clips found.")
            continue

        # Step 4: ffmpeg cuts the clips
        count = cut_clips(video_path, clips, video["title"])
        total_clips += count

    print("\n" + "="*55)
    print(f"  Done! {total_clips} shorts saved to → {OUTPUT_DIR}/")
    print("="*55 + "\n")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  python shorts_clipper.py @freecodecamp")
        print("  python shorts_clipper.py @TraversyMedia")
        print("  python shorts_clipper.py https://www.youtube.com/@fireship\n")
        sys.exit(0)

    run(sys.argv[1])
