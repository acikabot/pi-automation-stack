"""
Meet Kevin YouTube Summarizer Bot — Raspberry Pi Version
=========================================================
Headless version — no tray icon, no desktop notifications.
Runs as a systemd service on the Pi.

Checks for new Meet Kevin videos every hour.
Only processes videos published on or after June 4th 2026.
Skips YouTube Shorts automatically.

Logs everything to kevin_bot.log in the same folder.
All times displayed in UTC+2 (Macedonia local time).

Run modes:
  python kevin_bot.py         — normal mode (runs forever, checks hourly)
  python kevin_bot.py test    — test mode (summarizes latest video once, exits)
"""

import os
import re
import sys
import json
import time
import logging
import smtplib
import schedule
import threading
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

GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
KEVIN_NTFY      = os.environ.get("KEVIN_NTFY", "")

CUTOFF_DATE  = datetime(2026, 6, 4, tzinfo=timezone.utc)
CHANNEL_ID   = "UCUvvj5lwue7PspotMDjk5UA"
CHANNEL_RSS  = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
SEEN_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_videos.json")
LOG_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kevin_bot.log")
CHECK_INTERVAL_MINUTES = 60

TEST_MODE = len(sys.argv) > 1 and sys.argv[1] == "test"

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

# ─── Seen videos tracker ──────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("seen", []))
    return set()

def save_seen(seen: set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": list(seen), "last_updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)

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

def get_latest_videos(max_videos=15):
    feed   = feedparser.parse(CHANNEL_RSS)
    videos = []
    for entry in feed.entries[:max_videos]:
        vid_id = entry.get("yt_videoid", "")
        if not vid_id:
            raw    = entry.get("id", "")
            vid_id = raw.split(":")[-1] if ":" in raw else ""
        if vid_id:
            videos.append({
                "id":           vid_id,
                "title":        entry.get("title", "Unknown"),
                "link":         f"https://www.youtube.com/watch?v={vid_id}",
                "published":    entry.get("published", ""),
                "published_dt": parse_date(entry),
            })
    return videos

# ─── Shorts detection ─────────────────────────────────────────────────────────

def is_short(video_id: str) -> bool:
    try:
        r = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            allow_redirects=False,
            timeout=5
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Shorts check failed for {video_id}: {e}")
        return False

# ─── Transcript ───────────────────────────────────────────────────────────────

def get_transcript(video_id: str):
    try:
        api     = YouTubeTranscriptApi()
        fetched = api.fetch(video_id, languages=["en"])
        text    = " ".join(seg.text for seg in fetched)
        text    = re.sub(r"\[.*?\]", "", text)
        text    = re.sub(r"\s+", " ", text).strip()
        return text
    except TranscriptsDisabled:
        log.warning(f"Transcripts disabled: {video_id}")
    except NoTranscriptFound:
        log.warning(f"No English transcript: {video_id}")
    except Exception as e:
        log.warning(f"Transcript error: {e}")
    return None

# ─── Chunker ──────────────────────────────────────────────────────────────────

def chunk_transcript(text: str, max_chars=12000) -> list:
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

# ─── Summarizer ───────────────────────────────────────────────────────────────

def summarize(transcript: str, title: str, url: str) -> str:
    chunks = chunk_transcript(transcript)
    if len(chunks) == 1:
        return _summarize_full(chunks[0], title, url)
    log.info(f"Long video — {len(chunks)} chunks")
    partials = [_summarize_partial(c, title, i+1, len(chunks)) for i, c in enumerate(chunks)]
    return _combine(partials, title, url)

def _call_groq(prompt: str, max_tokens=2500) -> str:
    r = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        temperature=0.3,
        reasoning_effort="low",
    )
    return re.sub(r"<think>.*?</think>", "", r.choices[0].message.content, flags=re.DOTALL).strip()

def _summarize_full(transcript: str, title: str, url: str) -> str:
    return _call_groq(f"""You are summarizing a financial YouTube video by Meet Kevin (Kevin Paffrath), a US financial creator covering stocks, real estate, and markets.

VIDEO TITLE: {title}
VIDEO URL: {url}

FULL TRANSCRIPT:
{transcript}

Write a structured summary with exactly these sections:

MAIN THESIS
Kevin's central argument in 1-2 sentences.

KEY POINTS
The 5-8 most important points Kevin makes. Each gets 2-3 sentences including his reasoning and any data cited.

ACTIONABLE TAKEAWAYS
3-5 concrete things Kevin suggests viewers do or watch for.

RISKS OR CONCERNS MENTIONED
2-4 risks or warnings Kevin raised.

FORWARD-LOOKING STATEMENTS
Any predictions or upcoming catalysts Kevin mentioned.

SENTIMENT
One word (Bullish / Bearish / Mixed) + one sentence explaining the overall tone.

PLAIN ENGLISH SUMMARY
Explain the entire video in plain simple language as if telling a friend who knows nothing about finance. No jargon, no ticker symbols, no technical terms. 4-6 sentences. Anyone should immediately understand what Kevin was talking about and why it matters.

Be direct and specific. No filler. This summary replaces watching the video.""")

def _summarize_partial(transcript: str, title: str, n: int, total: int) -> str:
    return _call_groq(f"""Part {n} of {total} of a Meet Kevin financial video: "{title}"

TRANSCRIPT:
{transcript}

Extract key points: main arguments, predictions, data cited, risks, actionable advice.
Write 3-5 concise paragraphs.""", max_tokens=1300)

def _combine(partials: list, title: str, url: str) -> str:
    combined = "\n\n---\n\n".join(partials)
    return _call_groq(f"""Partial summaries of Meet Kevin video: "{title}" ({url})

{combined}

Write one unified structured summary:

MAIN THESIS — 1-2 sentences
KEY POINTS — 6-8 points, 2-3 sentences each
ACTIONABLE TAKEAWAYS — 3-5 concrete items
RISKS OR CONCERNS MENTIONED — 2-4 points
FORWARD-LOOKING STATEMENTS
SENTIMENT — one word + one sentence
PLAIN ENGLISH SUMMARY — 4-6 sentences, no jargon, explain like telling a friend""")

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(summary: str, title: str, url: str, published: str, test=False):
    local_time = fmt_local(datetime.now(timezone.utc))
    prefix     = "[TEST] " if test else ""
    subject    = f"{prefix}Meet Kevin: {title}"

    formatted = summary
    for plain, emoji in [
        ("MAIN THESIS",               "🎯 MAIN THESIS"),
        ("KEY POINTS",                "📌 KEY POINTS"),
        ("ACTIONABLE TAKEAWAYS",      "💡 ACTIONABLE TAKEAWAYS"),
        ("RISKS OR CONCERNS MENTIONED","⚠️ RISKS OR CONCERNS MENTIONED"),
        ("FORWARD-LOOKING STATEMENTS","🔮 FORWARD-LOOKING STATEMENTS"),
        ("SENTIMENT",                 "📊 SENTIMENT"),
        ("PLAIN ENGLISH SUMMARY",     "🧠 PLAIN ENGLISH SUMMARY"),
    ]:
        formatted = formatted.replace(plain, emoji)

    html = f"""<html><body style="font-family:Georgia,serif;max-width:680px;margin:auto;padding:24px;color:#1a1a1a;background:#fff;">
    {"<div style='background:#fff3cd;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px;'><b>TEST MODE</b> — seen_videos.json was not updated.</div>" if test else ""}
    <h2 style="border-bottom:2px solid #eee;padding-bottom:12px;">📹 Meet Kevin Summary</h2>
    <p style="font-size:18px;font-weight:bold;color:#1a1a2e;margin:0 0 6px;">{title}</p>
    <p style="font-size:13px;color:#888;margin:0 0 16px;">
      Published: {published} &nbsp;|&nbsp;
      <a href="{url}" style="color:#4f8ef7;">Watch on YouTube →</a>
    </p>
    <hr style="border:none;border-top:1px solid #eee;margin:0 0 16px;"/>
    <div style="font-size:15px;line-height:1.9;white-space:pre-wrap;">{formatted}</div>
    <hr style="margin-top:32px;border:none;border-top:1px solid #eee;"/>
    <p style="color:#aaa;font-size:12px;margin-top:8px;">
      {"[TEST] " if test else ""}Auto-summarized by Kevin Bot (Pi) • Groq Llama 3.3 70B • {local_time}
    </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(summary, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())

    log.info(f"Email sent: {subject}")

# ─── ntfy ─────────────────────────────────────────────────────────────────────

def send_ntfy(title: str, success=True, test=False):
    if not KEVIN_NTFY:
        return
    prefix = "[TEST] " if test else ""
    if success:
        ntfy_title = f"{prefix}Meet Kevin summarized"
        msg        = f"{title} — summary in your inbox."
        tags       = "youtube,white_check_mark"
        priority   = "high"
    else:
        ntfy_title = "Kevin Bot error"
        msg        = f"Failed: {title}"
        tags       = "warning"
        priority   = "urgent"
    try:
        requests.post(
            f"https://ntfy.sh/{KEVIN_NTFY}",
            headers={"Title": ntfy_title, "Priority": priority, "Tags": tags},
            data=msg.encode("utf-8"),
            timeout=10
        )
    except Exception as e:
        log.warning(f"ntfy error: {e}")

# ─── Process one video ────────────────────────────────────────────────────────

def process_video(video: dict, test=False) -> bool:
    log.info(f"Processing: {video['title']}")
    log.info(f"  URL: {video['link']}")
    log.info(f"  Published: {fmt_local(video['published_dt'])}")

    log.info("  Fetching transcript...")
    transcript = get_transcript(video["id"])
    if not transcript:
        log.warning("  No transcript — skipping.")
        return False

    words = len(transcript.split())
    log.info(f"  Transcript: {words:,} words")

    if words < 150:
        log.info(f"  Skipping — too short ({words} words), likely a Short.")
        return False

    log.info("  Generating summary with Groq...")
    summary = summarize(transcript, video["title"], video["link"])

    log.info("  Sending email...")
    send_email(summary, video["title"], video["link"], video["published"], test=test)
    send_ntfy(video["title"], success=True, test=test)
    return True

# ─── Core check ───────────────────────────────────────────────────────────────

def check_for_new_videos():
    now_str = local_now().strftime("%H:%M")
    log.info(f"[{now_str} {TZ_NAME}] Checking Meet Kevin's channel...")

    try:
        videos = get_latest_videos(max_videos=15)
        if not videos:
            log.warning("No videos found in RSS feed.")
            return

        seen      = load_seen()
        new_count = 0

        for video in videos:
            vid_id = video["id"]
            pub_dt = video["published_dt"]

            if vid_id in seen:
                continue

            if pub_dt < CUTOFF_DATE:
                log.info(f"Skipping (before cutoff): {video['title']}")
                seen.add(vid_id)
                continue

            if is_short(vid_id):
                log.info(f"Skipping (Short): {video['title']}")
                seen.add(vid_id)
                continue

            try:
                success = process_video(video, test=False)
                seen.add(vid_id)
                if success:
                    new_count += 1
            except Exception as e:
                log.error(f"Error: {e}")
                send_ntfy(video["title"], success=False)
                seen.add(vid_id)

        save_seen(seen)
        log.info(f"Done — {new_count} new video(s) summarized." if new_count else "No new videos.")

    except Exception as e:
        log.error(f"Check failed: {e}")
        send_ntfy("Kevin Bot", success=False)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 50)
    log.info("Kevin Bot (Pi) started")
    log.info(f"Timezone: {TZ_NAME} | Interval: every {CHECK_INTERVAL_MINUTES} min")
    log.info(f"Cutoff: {CUTOFF_DATE.strftime('%B %d %Y')}")
    log.info("=" * 50)

    if TEST_MODE:
        log.info("TEST MODE — summarizing latest non-Short video")
        videos = get_latest_videos(max_videos=15)
        if not videos:
            log.warning("No videos found.")
            return
        latest = next((v for v in videos if not is_short(v["id"])), None)
        if not latest:
            log.warning("Only Shorts found.")
            return
        log.info(f"Testing with: {latest['title']}")
        try:
            process_video(latest, test=True)
            log.info("Test complete.")
        except Exception as e:
            log.error(f"Test failed: {e}")
            send_ntfy(latest["title"], success=False, test=True)
        return

    # Normal mode — check immediately then every hour
    check_for_new_videos()
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(check_for_new_videos)
    log.info(f"Scheduler running — next check in {CHECK_INTERVAL_MINUTES} minutes.")

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
