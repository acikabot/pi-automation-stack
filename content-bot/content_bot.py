"""
Content Research Bot — Raspberry Pi Version
============================================
Monitors a list of YouTube channels for new videos.
Fetches transcripts, uses Groq AI to find Short-worthy clip candidates.
Sends a daily email with candidates ranked and categorized.

Schedule: Runs 3 times daily (08:00, 11:00, 14:00 local time)
Each run processes up to 3 channels worth of unprocessed videos.
Token-weighted batching keeps Groq rate limits safe.

Config:
  channels.json  — list of channels to monitor
  .env           — API keys

Run:
  python content_bot.py         — normal run (processes next batch)
  python content_bot.py test    — test mode (one channel, no seen update)
"""

import os
import re
import sys
import json
import time
import logging
import smtplib
import requests
import feedparser
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import parsedate_to_datetime
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled

load_dotenv()

# ─── Timezone ─────────────────────────────────────────────────────────────────

TZ_OFFSET = timedelta(hours=2)
TZ_NAME   = "UTC+2"

def local_now() -> datetime:
    return datetime.now(timezone.utc) + TZ_OFFSET

def fmt_local(dt: datetime) -> str:
    if dt.tzinfo:
        local = dt.astimezone(timezone.utc) + TZ_OFFSET
    else:
        local = dt + TZ_OFFSET
    return local.strftime("%d %b %Y, %H:%M") + f" ({TZ_NAME})"

# ─── Config ───────────────────────────────────────────────────────────────────

GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
EMAIL_SENDER       = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD     = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT    = os.environ["EMAIL_RECIPIENT"]
CONTENT_NTFY       = os.environ.get("CONTENT_NTFY", "")

# Max channels to process per run (token-weighted batching)
MAX_CHANNELS_PER_RUN  = 3
# Approximate max tokens per batch to stay safe under Groq limits
MAX_TOKENS_PER_BATCH  = 80000
# Estimated tokens per 1000 words of transcript
TOKENS_PER_1000_WORDS = 1350
# Only process videos uploaded in the last 24 hours
LOOKBACK_HOURS        = 25  # slightly over 24 to catch edge cases

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE  = os.path.join(BASE_DIR, "seen_videos.json")
LOG_FILE   = os.path.join(BASE_DIR, "content_bot.log")
CHAN_FILE  = os.path.join(BASE_DIR, "channels.json")

TEST_MODE  = len(sys.argv) > 1 and sys.argv[1] == "test"

client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ─── Channels config ──────────────────────────────────────────────────────────

def load_channels() -> list:
    """Load channel list from channels.json."""
    if not os.path.exists(CHAN_FILE):
        log.error(f"channels.json not found at {CHAN_FILE}")
        return []
    with open(CHAN_FILE, encoding="utf-8") as f:
        return json.load(f).get("channels", [])

# ─── Seen videos tracker ──────────────────────────────────────────────────────

def load_seen() -> dict:
    """Load seen videos dict — keyed by channel_id, values are lists of video_ids."""
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_seen(seen: dict):
    seen["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=2)

# ─── RSS feed ─────────────────────────────────────────────────────────────────

def parse_date(entry) -> datetime:
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        import calendar
        return datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=timezone.utc)
    pub = entry.get("published", "")
    if pub:
        try:
            return parsedate_to_datetime(pub).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)

def get_channel_videos(channel_id: str, max_videos=15) -> list:
    """Fetch latest videos from a channel via RSS."""
    url  = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    feed = feedparser.parse(url)
    vids = []
    for entry in feed.entries[:max_videos]:
        vid_id = entry.get("yt_videoid", "")
        if not vid_id:
            raw    = entry.get("id", "")
            vid_id = raw.split(":")[-1] if ":" in raw else ""
        if vid_id:
            vids.append({
                "id":           vid_id,
                "title":        entry.get("title", "Unknown"),
                "link":         f"https://www.youtube.com/watch?v={vid_id}",
                "published":    entry.get("published", ""),
                "published_dt": parse_date(entry),
            })
    return vids

# ─── Shorts detection ─────────────────────────────────────────────────────────

def is_short(video_id: str) -> bool:
    try:
        r = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            allow_redirects=False, timeout=5
        )
        return r.status_code == 200
    except Exception:
        return False

# ─── Transcript ───────────────────────────────────────────────────────────────

def format_timestamp(seconds: float) -> str:
    """Convert seconds to MM:SS format."""
    seconds = int(seconds)
    minutes = seconds // 60
    secs    = seconds % 60
    return f"{minutes:02d}:{secs:02d}"

def get_transcript(video_id: str):
    try:
        api     = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en"])
        # Build timestamped transcript — [MM:SS] marker at each segment
        lines = []
        for seg in fetched:
            ts   = format_timestamp(seg.start)
            text = re.sub(r"\[.*?\]", "", seg.text).strip()
            if text:
                lines.append(f"[{ts}] {text}")
        return "\n".join(lines)
    except TranscriptsDisabled:
        log.warning(f"  Transcripts disabled: {video_id}")
    except NoTranscriptFound:
        log.warning(f"  No English transcript: {video_id}")
    except Exception as e:
        log.warning(f"  Transcript error: {e}")
    return None

# ─── Token estimator ──────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    # Strip timestamps before counting words
    clean = re.sub(r"\[\d+:\d+\]", "", text)
    words = len(clean.split())
    return int(words * TOKENS_PER_1000_WORDS / 1000)

# ─── Chunker ──────────────────────────────────────────────────────────────────

def chunk_transcript(text: str, max_chars=5000) -> list:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    while len(text) > max_chars:
        cut = text[:max_chars].rfind(". ")
        cut = (cut + 1) if cut != -1 else max_chars
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks

# ─── Groq call ────────────────────────────────────────────────────────────────

def call_groq(prompt: str, max_tokens=1500) -> str:
    # Small delay between calls to respect rate limits
    time.sleep(8)
    r = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
        reasoning_effort="low",
    )
    return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content, flags=re.DOTALL).strip()

# ─── Analysis prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI content researcher helping find potential YouTube Shorts clips from long-form videos.

Your job is NOT to summarize the video. Your job is to identify every section of the transcript that could potentially be turned into an engaging YouTube Short, Instagram Reel, or TikTok.

I will personally review your suggestions and decide which clips to use. Your job is to find potential — not make final decisions.

WHAT MAKES A GOOD CLIP — look for moments containing one or more of:
- STRONG OPINION: confident claim, hot take, criticism of common beliefs
- CONTRARIAN: challenges common advice, "everyone says X but actually Y"
- SURPRISING FACT: makes someone think "wait, really?"
- ACTIONABLE ADVICE: practical tip someone can immediately apply
- MISTAKE / WARNING: "you're doing X wrong", bad habits, consequences
- MYTH BUST: challenges something commonly believed
- STORY: personal experience, failure, transformation, unexpected event
- EMOTIONAL MOMENT: funny, frustrating, exciting, shocking, strong personality
- DEBATE POTENTIAL: statement likely to provoke comments or disagreement
- CURIOSITY GAP: statement that makes you want to hear the explanation
- STRONG SOUNDBITE: sentence that can stand alone without context
- INTERESTING EXPLANATION: complicated concept explained simply or unusually well

DO NOT RETURN:
- Generic introductions or greetings
- Sponsor segments or self-promotion
- Repetitive explanations
- Basic information with no interesting angle
- Generic motivational statements unless unusually specific
- Moments only interesting because you know the surrounding context

OVER-SELECT RATHER THAN UNDER-SELECT:
Find ALL reasonably promising moments. If a video has 12 candidates, return 12.
I decide what to use. When uncertain, include it.

The transcript below has [MM:SS] timestamp markers at the start of every line.
These are the exact timestamps from the YouTube video.

CRITICAL INSTRUCTION: For every clip you identify, you MUST include the Timestamp field.
Look at the [MM:SS] marker on the line where the clip starts and copy it exactly.
If you cannot find a timestamp for a clip, do not include that clip.

FOR EACH CLIP return exactly this format with no deviations:

CLIP [number]
Timestamp: [copy the MM:SS marker from the transcript line where this clip begins]
Category: [one of the categories above in caps]
Score: [1-10 overall short potential]
Opening: "[first 1-2 sentences of the clip copied verbatim from the transcript]"
Duration: [rough estimate: "~30 sec" / "~1 min" / "~2 min"]
Why: [one sentence on why this could work as a Short]

Example of correct output:
CLIP 1
Timestamp: 04:32
Category: CONTRARIAN
Score: 8/10
Opening: "Most people think you need to train every day but that is completely wrong."
Duration: ~45 sec
Why: Challenges common belief and will generate debate in comments.

After all clips, add a section:
TOP PICKS: [comma-separated clip numbers that are the strongest candidates]"""

def analyze_transcript(transcript: str, video_title: str, channel_name: str, niche: str, chunk_num=1, total_chunks=1) -> str:
    chunk_note = f" (part {chunk_num} of {total_chunks})" if total_chunks > 1 else ""
    prompt = f"""{SYSTEM_PROMPT}

NICHE: {niche}
CHANNEL: {channel_name}
VIDEO TITLE: {video_title}{chunk_note}

TRANSCRIPT:
{transcript}

Find all clip candidates now."""
    return call_groq(prompt)

def combine_analyses(parts: list, video_title: str, channel_name: str) -> str:
    combined = "\n\n---PART BREAK---\n\n".join(parts)
    if len(combined) > 6000:
        combined = combined[:6000] + "\n\n[...additional candidates truncated to fit token limit...]"
    prompt = f"""You analyzed a long video in {len(parts)} parts. Here are the clip candidates found across all parts:

VIDEO: {video_title}
CHANNEL: {channel_name}

{combined}

Now produce one unified list. Re-number all clips sequentially. Remove any duplicates.
Keep the same format:

CLIP [number]
Timestamp: [MM:SS]
Category: [category]
Score: [1-10]
Opening: "[opening sentence]"
Duration: [duration]
Why: [one sentence]

Then at the end:
TOP PICKS: [clip numbers]"""
    return call_groq(prompt, max_tokens=1500)

# ─── Process one video ────────────────────────────────────────────────────────

def process_video(video: dict, channel: dict) -> dict | None:
    """Returns analysis dict or None if skipped."""
    log.info(f"  Processing: {video['title']}")
    log.info(f"  URL: {video['link']}")

    if is_short(video["id"]):
        log.info("  Skipping — YouTube Short.")
        return None

    transcript = get_transcript(video["id"])
    if not transcript:
        log.warning("  No transcript — skipping.")
        return None

    words = len(re.sub(r"\[\d+:\d+\]", "", transcript).split())
    log.info(f"  Transcript: {words:,} words (~{estimate_tokens(transcript):,} tokens)")

    if words < 150:
        log.info("  Skipping — too short, likely a Short.")
        return None

    chunks = chunk_transcript(transcript)
    log.info(f"  Analyzing in {len(chunks)} chunk(s)...")

    niche = channel.get("niche", "general")

    if len(chunks) == 1:
        analysis = analyze_transcript(chunks[0], video["title"], channel["name"], niche)
    else:
        parts = []
        for i, chunk in enumerate(chunks):
            log.info(f"  Chunk {i+1}/{len(chunks)}...")
            part = analyze_transcript(chunk, video["title"], channel["name"], niche, i+1, len(chunks))
            parts.append(part)
            time.sleep(10)  # extra pause between chunks
        analysis = combine_analyses(parts, video["title"], channel["name"])

    return {
        "channel_name": channel["name"],
        "video_title":  video["title"],
        "video_url":    video["link"],
        "published":    fmt_local(video["published_dt"]),
        "analysis":     analysis,
    }

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(results: list, test=False):
    if not results:
        log.info("No results to email.")
        return

    date_str = local_now().strftime("%A, %d %b %Y")
    prefix   = "[TEST] " if test else ""
    subject  = f"{prefix}Content Research — {date_str}"

    # Build plain text body
    lines = [f"Content Research Report — {date_str}\n"]
    lines.append("=" * 50)

    for r in results:
        lines.append(f"\n📺 {r['channel_name'].upper()}")
        lines.append(f"   {r['video_title']}")
        lines.append(f"   {r['video_url']}")
        lines.append(f"   Published: {r['published']}")
        lines.append("")
        lines.append(r["analysis"])
        lines.append("\n" + "─" * 50)

    plain = "\n".join(lines)

    # Build HTML body
    html_sections = ""
    for r in results:
        # Format analysis — bold CLIP headers
        formatted = r["analysis"]
        formatted = re.sub(r"(CLIP \d+)", r"<b>\1</b>", formatted)
        formatted = re.sub(r"(TOP PICKS:.*)", r"<b style='color:#4f8ef7'>\1</b>", formatted)

        html_sections += f"""
        <div style="margin-bottom:32px;border-left:3px solid #cc0000;padding-left:16px;">
          <p style="font-size:13px;color:#888;margin:0 0 2px;">📺 {r['channel_name']}</p>
          <p style="font-size:16px;font-weight:bold;color:#1a1a2e;margin:0 0 4px;">{r['video_title']}</p>
          <p style="font-size:12px;color:#888;margin:0 0 12px;">
            <a href="{r['video_url']}" style="color:#4f8ef7;">Watch on YouTube →</a>
            &nbsp;|&nbsp; Published: {r['published']}
          </p>
          <div style="font-size:14px;line-height:1.8;white-space:pre-wrap;background:#f8f9ff;padding:16px;border-radius:8px;">{formatted}</div>
        </div>"""

    html = f"""<html><body style="font-family:Georgia,serif;max-width:720px;margin:auto;padding:24px;color:#1a1a1a;background:#fff;">
    {"<div style='background:#fff3cd;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px;'><b>TEST MODE</b></div>" if test else ""}
    <h2 style="border-bottom:2px solid #eee;padding-bottom:12px;">🎬 Content Research — {date_str}</h2>
    <p style="color:#888;font-size:13px;">{len(results)} video(s) analyzed</p>
    {html_sections}
    <hr style="margin-top:32px;border:none;border-top:1px solid #eee;"/>
    <p style="color:#aaa;font-size:12px;">Content Research Bot • Groq Llama 3.3 70B • {fmt_local(datetime.now(timezone.utc))}</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    recipients = [x.strip() for x in EMAIL_RECIPIENT.split(",")]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.sendmail(EMAIL_SENDER, recipients, msg.as_string())

    log.info(f"Email sent: {subject}")

# ─── ntfy ─────────────────────────────────────────────────────────────────────

def send_ntfy(message: str, title="Content Research", success=True):
    if not CONTENT_NTFY:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{CONTENT_NTFY}",
            headers={
                "Title":    title,
                "Priority": "high" if success else "urgent",
                "Tags":     "video_camera,white_check_mark" if success else "warning",
            },
            data=message.encode("utf-8"),
            timeout=10
        )
    except Exception as e:
        log.warning(f"ntfy error: {e}")

# ─── Batch selector (token-weighted) ─────────────────────────────────────────

def select_batch(channels: list, seen: dict, cutoff: datetime) -> list:
    """
    Select up to MAX_CHANNELS_PER_RUN channels that have unprocessed videos.
    Token-weighted: skips channels whose estimated transcript size would blow the batch budget.
    Returns list of (channel, [new_videos]) tuples.
    """
    batch        = []
    batch_tokens = 0

    for channel in channels:
        if len(batch) >= MAX_CHANNELS_PER_RUN:
            break

        cid      = channel["id"]
        seen_ids = set(seen.get(cid, []))

        try:
            videos = get_channel_videos(cid)
        except Exception as e:
            log.warning(f"RSS fetch failed for {channel['name']}: {e}")
            continue

        # Filter to new videos within lookback window
        new_vids = [
            v for v in videos
            if v["id"] not in seen_ids
            and v["published_dt"] >= cutoff
        ]

        if not new_vids:
            log.info(f"No new videos: {channel['name']}")
            continue

        # Estimate token cost for this channel's videos
        est_tokens = len(new_vids) * 12000  # rough average per video
        if batch_tokens + est_tokens > MAX_TOKENS_PER_BATCH and batch:
            log.info(f"Skipping {channel['name']} this batch — token budget reached.")
            continue

        batch.append((channel, new_vids))
        batch_tokens += est_tokens
        log.info(f"Queued: {channel['name']} ({len(new_vids)} new video(s))")

    return batch

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 50)
    log.info(f"Content Research Bot — {'TEST MODE' if TEST_MODE else 'normal run'}")
    log.info(f"Time: {local_now().strftime('%H:%M')} {TZ_NAME}")
    log.info("=" * 50)

    channels = load_channels()
    if not channels:
        log.error("No channels configured. Check channels.json.")
        return

    log.info(f"Loaded {len(channels)} channel(s).")

    # ── TEST MODE ──────────────────────────────────────────────────────────────
    if TEST_MODE:
        log.info("TEST MODE — processing first available video from first channel.")
        channel = channels[0]
        videos  = get_channel_videos(channel["id"], max_videos=15)
        # Find first non-Short with a transcript
        target = None
        for v in videos:
            if not is_short(v["id"]):
                target = v
                break
        if not target:
            log.warning("No suitable video found for test.")
            return
        log.info(f"Test video: {target['title']}")
        result = process_video(target, channel)
        if result:
            send_email([result], test=True)
            send_ntfy(f"Test complete — {target['title'][:60]}", success=True)
            log.info("Test complete.")
        else:
            log.warning("Test video could not be processed.")
        return

    # ── NORMAL MODE ────────────────────────────────────────────────────────────
    seen    = load_seen()
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    log.info(f"Selecting batch (max {MAX_CHANNELS_PER_RUN} channels, ~{MAX_TOKENS_PER_BATCH:,} token budget)...")
    batch = select_batch(channels, seen, cutoff)

    if not batch:
        log.info("No new videos to process in this batch.")
        return

    log.info(f"Processing {len(batch)} channel(s)...")
    results = []

    for channel, new_vids in batch:
        cid      = channel["id"]
        seen_ids = seen.get(cid, [])

        for video in new_vids:
            try:
                result = process_video(video, channel)
                if result:
                    results.append(result)
            except Exception as e:
                log.error(f"Error on {video['title']}: {e}")
            finally:
                # Mark as seen regardless of success to avoid retrying broken videos
                if video["id"] not in seen_ids:
                    seen_ids.append(video["id"])

        seen[cid] = seen_ids

    save_seen(seen)

    if results:
        send_email(results)
        send_ntfy(
            f"{len(results)} video(s) analyzed — clips in your inbox.",
            title="Content Research ready"
        )
        log.info(f"Done — {len(results)} video(s) analyzed.")
    else:
        log.info("No usable videos found in this batch.")

if __name__ == "__main__":
    main()
