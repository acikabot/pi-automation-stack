"""
Dashboard configuration.

ADDING A NEW BOT:
  Append one dict to the BOTS list below. Nothing else needs to change —
  the API, the UI, the status rail, and the log viewer all read from this list.

Fields:
  id        Short slug used in URLs and the DOM. Lowercase, no spaces.
  name      Display name shown in the UI.
  service   systemd unit name, without the .service suffix.
  timer     systemd timer name (without .timer), or None if the bot runs continuously.
  dir       Absolute path to the bot's folder.
  script    Python entrypoint filename inside that folder.
  log       Log filename inside that folder.
  kind      "resident"  — always running, controlled with start/stop/restart
            "scheduled" — oneshot run fired by a timer
  runs      List of manual run modes exposed as buttons. Each is (label, argv_suffix).
            An empty string means run the script with no extra argument.
  config    Optional path to a JSON config file the dashboard can edit (content bot only).
"""

BASE_DIR = "/home/acika/bots"

BOTS = [
    {
        "id":      "kevin",
        "name":    "Kevin Bot",
        "service": "kevin-bot",
        "timer":   None,
        "dir":     f"{BASE_DIR}/kevin-bot",
        "script":  "kevin_bot.py",
        "log":     "kevin_bot.log",
        "kind":    "resident",
        "runs":    [("Test run", "test")],
        "config":  None,
        "blurb":   "Summarizes new videos from one channel, hourly.",
    },
    {
        "id":      "news",
        "name":    "News Bot",
        "service": "news-bot",
        "timer":   "news-bot",
        "dir":     f"{BASE_DIR}/news-bot",
        "script":  "news_bot.py",
        "log":     "news_bot.log",
        "kind":    "scheduled",
        "runs":    [("Morning brief", "morning"), ("Evening recap", "evening")],
        "config":  None,
        "blurb":   "Morning brief and evening recap, twice daily.",
    },
    {
        "id":      "content",
        "name":    "Content Bot",
        "service": "content-bot",
        "timer":   "content-bot",
        "dir":     f"{BASE_DIR}/content-bot",
        "script":  "content_bot.py",
        "log":     "content_bot.log",
        "kind":    "scheduled",
        "runs":    [("Full run", ""), ("Test run", "test")],
        "config":  f"{BASE_DIR}/content-bot/channels.json",
        "blurb":   "Scans channels for clip candidates, three times daily.",
    },
]

# The updater is not a bot, but the dashboard surfaces it on the System page.
UPDATER = {
    "service": "bot-updater",
    "timer":   "bot-updater",
    "log":     f"{BASE_DIR}/updater/updater.log",
}

# Whitelisted system actions. Nothing outside this map can be executed.
SYSTEM_ACTIONS = {
    "reboot":       ["sudo", "systemctl", "reboot"],
    "shutdown":     ["sudo", "systemctl", "poweroff"],
    "update-now":   ["sudo", "systemctl", "start", "bot-updater.service"],
}

HOST = "0.0.0.0"
PORT = 5000

# Flip to True and set a user/password when you put this behind Cloudflare.
# Nothing else needs changing — the dependency is already wired into every route.
AUTH_ENABLED  = False
AUTH_USER     = "admin"
AUTH_PASSWORD = "change-me"
