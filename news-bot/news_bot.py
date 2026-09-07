"""
Daily News Report Bot
Runs on GitHub Actions — completely free
Uses: Groq API (free, no credit card) + RSS feeds (free) + Gmail + ntfy

Four sections:
  1. World News — summary style
  2. Iran Conflict — full length, detailed
  3. Tech & AI — full length
  4. Forex & Markets — direct, 4-6 sentences per instrument (EUR/USD, GBP/JPY, Gold, Oil, Crypto/BTC) + DXY one-liner
"""

import os
from dotenv import load_dotenv
load_dotenv()
import sys
import re
import smtplib
import requests
import socket
import feedparser

socket.setdefaulttimeout(15)
from openai import OpenAI
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ─── Config from GitHub Secrets ──────────────────────────────────────────────

GROQ_API_KEY    = os.environ["GROQ_API_KEY"]
EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT = os.environ["EMAIL_RECIPIENT"]
NEWS_BOT_NTFY   = os.environ.get("NEWS_BOT_NTFY", "")

PROMPT_DIR      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

REPORT_TYPE = sys.argv[1] if len(sys.argv) > 1 else (
    "morning" if datetime.now().hour < 12 else "evening"
)

client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# ─── RSS Feed Sources ─────────────────────────────────────────────────────────

RSS_FEEDS = {
    "World News": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.reuters.com/reuters/worldNews",
        "https://rss.cnn.com/rss/edition_world.rss",
    ],
    "Iran Conflict": [
        "https://feeds.aljazeera.com/aljazeera/stories",
        "https://www.jpost.com/rss/rssfeedsfrontpage.aspx",
        "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
        "https://feeds.reuters.com/reuters/worldNews",
    ],
    "Tech & AI": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
        "https://www.nasdaq.com/feed/rssoutbound?category=Technology",
    ],
    "Forex & Markets": [
        "https://www.forexlive.com/feed/news",
        "https://www.investing.com/rss/news_25.rss",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://finance.yahoo.com/rss/2.0/headline?s=NVDA,AAPL,MSFT,TSLA,META&region=US&lang=en-US",
    ],
    "Crypto": [
        "https://cointelegraph.com/rss",
        "https://coindesk.com/arc/outboundfeeds/rss/",
        "https://cryptonews.com/news/feed/",
    ],
}

# ─── Fetch RSS headlines ───────────────────────────────────────────────────────

def fetch_headlines(max_per_feed=6, iran_max=8):
    all_news = {}
    for category, feeds in RSS_FEEDS.items():
        headlines = []
        limit = iran_max if category == "Iran Conflict" else max_per_feed
        for url in feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:limit]:
                    title   = entry.get("title", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", summary)[:300]
                    if title:
                        headlines.append(f"- {title}: {summary}")
            except Exception as e:
                print(f"Warning: Could not fetch {url}: {e}")
        all_news[category] = headlines[:16] if category == "Iran Conflict" else headlines[:12]
    return all_news

# ─── Build prompt ─────────────────────────────────────────────────────────────

def load_prompt(name: str) -> str:
    """
    Read a prompt template from prompts/<name>.txt.

    Read at call time so dashboard edits apply to the next scheduled run.
    """
    with open(os.path.join(PROMPT_DIR, f"{name}.txt"), encoding="utf-8") as f:
        return f.read()

def build_prompt(headlines: dict, report_type: str) -> str:
    """
    Wrap the editable report prompt with the date and the fetched headlines.

    The prompt file holds only the creative part — sections, ordering, tone.
    The scaffolding below is fixed so an edit in the dashboard can't detach the
    report from its data.
    """
    date_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    news_block = ""
    for category, items in headlines.items():
        news_block += f"\n### {category}\n"
        news_block += "\n".join(items) if items else "No headlines available."
        news_block += "\n"

    name = "morning" if report_type == "morning" else "evening"
    return f"""Today is {date_str}.

{load_prompt(name)}

RAW HEADLINES:
{news_block}

Write the report now. Facts only. Zero filler."""

# ─── Groq API call ────────────────────────────────────────────────────────────

def generate_report(prompt: str) -> str:
    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1800,
        temperature=0.4,
        reasoning_effort="low",
    )
    return re.sub(r"<think>.*?</think>", "", response.choices[0].message.content, flags=re.DOTALL).strip()

# ─── Send Email ───────────────────────────────────────────────────────────────

def send_email(report_text: str, report_type: str):
    date_str = datetime.now(timezone.utc).strftime("%A, %d %b %Y")
    subject  = (
        f"Morning Brief — {date_str}"
        if report_type == "morning"
        else f"Evening Recap — {date_str}"
    )

    html_body = f"""
    <html>
    <body style="font-family: Georgia, serif; max-width: 680px; margin: auto;
                 padding: 24px; color: #1a1a1a; background: #ffffff;">
      <h2 style="color: #1a1a1a; border-bottom: 2px solid #eee; padding-bottom: 12px;">
        {subject}
      </h2>
      <div style="font-size: 15px; line-height: 1.8; white-space: pre-wrap;">
{report_text}
      </div>
      <hr style="margin-top: 32px; border: none; border-top: 1px solid #eee;"/>
      <p style="color: #aaa; font-size: 12px; margin-top: 8px;">
        Generated automatically - News Bot RasPi + Groq (Llama 3.3 70B) • {date_str}
      </p>
    </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECIPIENT
    msg.attach(MIMEText(report_text, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENT, msg.as_string())

    print(f"Email sent: {subject}")

# ─── Send ntfy push notification ─────────────────────────────────────────────

def send_ntfy(report_type: str, success: bool = True):
    if not NEWS_BOT_NTFY:
        print("ntfy skipped — NEWS_BOT_NTFY secret not set.")
        return

    date_str = datetime.now(timezone.utc).strftime("%d %b, %H:%M UTC")

    if success:
        title    = "Morning Brief ready" if report_type == "morning" else "Evening Recap ready"
        message  = f"Your report is in your inbox. ({date_str})"
        tags     = "sunrise,white_check_mark" if report_type == "morning" else "night_with_stars,white_check_mark"
        priority = "high"
    else:
        title    = "News Bot failed"
        message  = f"Something went wrong. Check GitHub Actions. ({date_str})"
        tags     = "warning"
        priority = "urgent"

    try:
        requests.post(
            f"https://ntfy.sh/{NEWS_BOT_NTFY}",
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     tags,
            },
            data=message.encode("utf-8"),
            timeout=10
        )
        print(f"ntfy notification sent: {title}")
    except Exception as e:
        print(f"ntfy warning: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"Running {REPORT_TYPE} report at {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    try:
        print("Fetching RSS headlines...")
        headlines = fetch_headlines()
        total = sum(len(v) for v in headlines.values())
        print(f"Fetched {total} headlines across {len(headlines)} categories.")

        print("Generating report with Groq (Llama 3.3 70B)...")
        prompt = build_prompt(headlines, REPORT_TYPE)
        report = generate_report(prompt)

        print("Sending email...")
        send_email(report, REPORT_TYPE)

        send_ntfy(REPORT_TYPE, success=True)
        print("Done.")

    except Exception as e:
        print(f"Error: {e}")
        send_ntfy(REPORT_TYPE, success=False)
        raise

if __name__ == "__main__":
    main()
