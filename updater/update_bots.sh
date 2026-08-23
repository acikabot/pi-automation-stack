#!/bin/bash
# Bot Dependency + System Updater
# Runs every 5 days via systemd timer.
# 1. Upgrades OS packages (apt)
# 2. Upgrades Python packages in each bot's venv
# 3. Restarts always-running bots
# 4. Sends ntfy notification, flags if a reboot is needed

set -uo pipefail

BOTS_DIR="/home/acika/bots"
LOG_FILE="$BOTS_DIR/updater/updater.log"
ENV_FILE="$BOTS_DIR/updater/.env"

UPDATER_NTFY=""
if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE"
fi

echo "==================================================" >> "$LOG_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') Updater started" >> "$LOG_FILE"

# ─── 1. System packages ────────────────────────────────────────────────────
echo "--- System update (apt) ---" >> "$LOG_FILE"
sudo apt update >> "$LOG_FILE" 2>&1
sudo apt upgrade -y >> "$LOG_FILE" 2>&1
sudo apt autoremove -y >> "$LOG_FILE" 2>&1

# ─── 2. Bot Python packages ────────────────────────────────────────────────
BOTS=("kevin-bot" "news-bot" "content-bot")
SUMMARY=""

for bot in "${BOTS[@]}"; do
    BOT_PATH="$BOTS_DIR/$bot"
    if [ -d "$BOT_PATH/venv" ] && [ -f "$BOT_PATH/requirements.txt" ]; then
        echo "--- Updating $bot ---" >> "$LOG_FILE"
        source "$BOT_PATH/venv/bin/activate"
        pip install --upgrade -r "$BOT_PATH/requirements.txt" >> "$LOG_FILE" 2>&1
        deactivate
        SUMMARY="${SUMMARY}${bot}: updated. "
    else
        echo "Skipping $bot — venv or requirements.txt not found" >> "$LOG_FILE"
        SUMMARY="${SUMMARY}${bot}: skipped. "
    fi
done

# ─── 3. Restart always-running bots ────────────────────────────────────────
echo "Restarting always-running bots..." >> "$LOG_FILE"
sudo systemctl restart kevin-bot >> "$LOG_FILE" 2>&1
sudo systemctl restart news-bot >> "$LOG_FILE" 2>&1

echo "$(date '+%Y-%m-%d %H:%M:%S') Updater finished" >> "$LOG_FILE"

# ─── 4. Notification — flag if reboot is required ──────────────────────────
REBOOT_MSG=""
if [ -f /var/run/reboot-required ]; then
    REBOOT_MSG=" ⚠️ Reboot required — a system update needs a restart to take effect."
    echo "Reboot required." >> "$LOG_FILE"
fi

if [ -n "$UPDATER_NTFY" ]; then
    curl -s -H "Title: Bot Updater" -H "Priority: default" -H "Tags: package" \
         -d "System + dependency update complete. $SUMMARY$REBOOT_MSG" \
         "https://ntfy.sh/$UPDATER_NTFY" > /dev/null
fi
