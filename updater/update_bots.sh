#!/usr/bin/env bash
# System + dependency updater for everything running on this Pi.
# Runs every 5 days via bot-updater.timer.
#
# What it does, per project:
#   1. snapshots the exact package versions that are working right now
#   2. upgrades the packages in that project's venv
#   3. runs the project's tests, if it has any
#   4. restarts the service only if something actually changed
#   5. checks the service came back — and if anything failed, puts the old
#      versions back and restarts again, so a bad release can't leave a
#      service down until somebody notices
#
# Timer-driven bots (news-bot, content-bot) get their packages updated but are
# never restarted: restarting those units starts a run.
#
# Usage:
#   ./update_bots.sh                 normal run (system + every project)
#   ./update_bots.sh --dry-run       show what would change, install nothing
#   ./update_bots.sh --only avifly   one project, skipping the system update
#   ./update_bots.sh --skip-system   projects only
#
# Sudo: this user currently has blanket passwordless sudo. When that is
# tightened, /etc/sudoers.d/bot-updater needs these instead of the apt lines:
#   /usr/bin/apt-get update, /usr/bin/apt-get full-upgrade -y, /usr/bin/apt-get autoremove -y
#   /bin/systemctl restart kevin-bot.service pidash.service avifly.service

set -uo pipefail

# ─── Configuration ──────────────────────────────────────────────────────────
BOTS_DIR="/home/acika/bots"
UPDATER_DIR="$BOTS_DIR/updater"
LOG_FILE="$UPDATER_DIR/updater.log"
SNAPSHOT_DIR="$UPDATER_DIR/snapshots"
LOCK_FILE="$UPDATER_DIR/.update.lock"
ENV_FILE="$UPDATER_DIR/.env"

LOG_MAX_BYTES=$((1024 * 1024))   # rotate the log past 1 MB, keep one old copy
PIP_TIMEOUT=900
TEST_TIMEOUT=900
APT_TIMEOUT=1800
HEALTH_GRACE=4                   # seconds to let a service settle before checking

# name | directory | systemd unit | restart: always/never | health URL | test command
PROJECTS=(
  "kevin-bot|$BOTS_DIR/kevin-bot|kevin-bot.service|always||"
  "news-bot|$BOTS_DIR/news-bot|news-bot.service|never||"
  "content-bot|$BOTS_DIR/content-bot|content-bot.service|never||"
  "pidash|/home/acika/pi-dash|pidash.service|always|http://127.0.0.1:5000/healthz|make check"
  "avifly|/home/acika/avifly-tracker|avifly.service|always|http://127.0.0.1:8000/healthz|make check"
)

UPDATER_NTFY=""
[ -f "$ENV_FILE" ] && source "$ENV_FILE"

DRY_RUN=false
SKIP_SYSTEM=false
ONLY=""

# ─── Plumbing ───────────────────────────────────────────────────────────────
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE"; }
log_raw() { tee -a "$LOG_FILE" >/dev/null; }   # for command output

rotate_log() {
  [ -f "$LOG_FILE" ] || return 0
  local size
  size=$(stat -c %s "$LOG_FILE")
  if [ "$size" -gt "$LOG_MAX_BYTES" ]; then
    mv "$LOG_FILE" "$LOG_FILE.1"
    log "Rotated the log (the previous one is updater.log.1)"
  fi
}

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --skip-system) SKIP_SYSTEM=true ;;
    --only) ONLY="${2:-}"; SKIP_SYSTEM=true; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$SNAPSHOT_DIR"

# One run at a time: if the last one is somehow still going, leave it alone.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another run is still in progress — exiting."
  exit 0
fi

SUMMARY=()      # one line per project, for the notification
FAILURES=()     # whatever went wrong

# ─── Helpers ────────────────────────────────────────────────────────────────
venv_pip() {  # → the project's pip, whether its venv is called venv or .venv
  local dir="$1"
  for venv in "$dir/.venv" "$dir/venv"; do
    [ -x "$venv/bin/pip" ] && { echo "$venv/bin/pip"; return 0; }
  done
  return 1
}

requirements_file() {  # prefer requirements.txt, else whatever *requirements*.txt exists
  local dir="$1"
  [ -f "$dir/requirements.txt" ] && { echo "$dir/requirements.txt"; return 0; }
  local found
  found=$(find "$dir" -maxdepth 1 -name '*requirements*.txt' ! -name '*-dev.txt' | sort | head -1)
  [ -n "$found" ] && { echo "$found"; return 0; }
  return 1
}

# Which packages moved, as "name 1.2.3→1.3.0", comparing two pip freeze files.
package_changes() {
  local before="$1" after="$2"
  awk -F'==' '
    NR == FNR { was[tolower($1)] = $2; next }
    {
      name = tolower($1)
      if (!(name in was))       { printf "%s %s (new)\n", $1, $2 }
      else if (was[name] != $2) { printf "%s %s→%s\n", $1, was[name], $2 }
    }' "$before" "$after"
}

service_is_healthy() {  # unit is running, and answers on its health URL if it has one
  local unit="$1" url="$2"
  systemctl is-active --quiet "$unit" || return 1
  [ -z "$url" ] && return 0
  local code
  code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$url" 2>/dev/null)
  # Anything but "no answer" or a server error means it is up and serving.
  [ -n "$code" ] && [ "$code" != "000" ] && [ "${code:0:1}" != "5" ]
}

restore_snapshot() {
  local pip="$1" snapshot="$2" name="$3"
  log "  Putting $name back on the versions from before this run"
  timeout "$PIP_TIMEOUT" "$pip" install -q -r "$snapshot" 2>&1 | log_raw
}

# ─── 1. System packages ─────────────────────────────────────────────────────
system_update() {
  log "── System packages (apt-get) ──"
  if $DRY_RUN; then
    sudo apt-get update -qq 2>&1 | log_raw
    local pending
    pending=$(apt list --upgradable 2>/dev/null | grep -c upgradable)
    log "  Dry run: $((pending > 0 ? pending - 1 : 0)) package(s) would be upgraded"
    SUMMARY+=("system: dry run")
    return
  fi

  export DEBIAN_FRONTEND=noninteractive
  local ok=true
  timeout "$APT_TIMEOUT" sudo -E apt-get update -qq 2>&1 | log_raw || ok=false
  # full-upgrade so kernel and firmware updates, which pull in new packages, land too.
  timeout "$APT_TIMEOUT" sudo -E apt-get -y -qq \
      -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
      full-upgrade 2>&1 | log_raw || ok=false
  timeout "$APT_TIMEOUT" sudo -E apt-get -y -qq autoremove 2>&1 | log_raw || ok=false

  if $ok; then
    SUMMARY+=("system: updated")
  else
    SUMMARY+=("system: FAILED")
    FAILURES+=("system")
    log "  apt-get reported a problem — see the log above"
  fi
}

# ─── 2. One project ─────────────────────────────────────────────────────────
update_project() {
  local name="$1" dir="$2" unit="$3" restart="$4" health="$5" test_cmd="$6"
  log "── $name ──"

  local pip requirements
  if ! pip=$(venv_pip "$dir"); then
    log "  No virtualenv in $dir — skipped"
    SUMMARY+=("$name: skipped (no venv)")
    return
  fi
  if ! requirements=$(requirements_file "$dir"); then
    log "  No requirements file in $dir — skipped"
    SUMMARY+=("$name: skipped (no requirements)")
    return
  fi

  if $DRY_RUN; then
    local would
    would=$(timeout "$PIP_TIMEOUT" "$pip" install --upgrade --dry-run -r "$requirements" 2>/dev/null \
            | grep -E "^Would install" | sed 's/^Would install //')
    log "  Would install: ${would:-nothing new}"
    if [ -n "$would" ]; then
      SUMMARY+=("$name: would update $would")
    else
      SUMMARY+=("$name: up to date")
    fi
    return
  fi

  # Snapshot what works right now, so a bad upgrade can be undone.
  local before="$SNAPSHOT_DIR/$name.before.txt" after="$SNAPSHOT_DIR/$name.after.txt"
  "$pip" freeze > "$before" 2>/dev/null

  if ! timeout "$PIP_TIMEOUT" "$pip" install -q --upgrade -r "$requirements" 2>&1 | log_raw; then
    log "  pip failed — putting the old versions back"
    restore_snapshot "$pip" "$before" "$name"
    SUMMARY+=("$name: PIP FAILED")
    FAILURES+=("$name")
    return
  fi

  "$pip" freeze > "$after" 2>/dev/null
  local changes
  changes=$(package_changes "$before" "$after")
  if [ -z "$changes" ]; then
    log "  Already up to date — not restarting"
    SUMMARY+=("$name: no change")
    return
  fi
  log "  Updated: $(echo "$changes" | tr '\n' ' ')"

  # Dependency conflicts are worth knowing about even when pip itself succeeded.
  "$pip" check 2>&1 | grep -v "No broken requirements" | log_raw

  # Tests, where a project has them: a red suite sends the upgrade back.
  if [ -n "$test_cmd" ]; then
    log "  Running tests: $test_cmd"
    if ! (cd "$dir" && timeout "$TEST_TIMEOUT" bash -c "$test_cmd") 2>&1 | log_raw; then
      restore_snapshot "$pip" "$before" "$name"
      SUMMARY+=("$name: TESTS FAILED, rolled back")
      FAILURES+=("$name")
      return
    fi
    log "  Tests passed"
  fi

  if [ "$restart" != "always" ]; then
    # Timer-driven: restarting the unit would start a run, so leave it alone.
    SUMMARY+=("$name: updated (next scheduled run picks it up)")
    return
  fi

  log "  Restarting $unit"
  sudo systemctl restart "$unit" 2>&1 | log_raw
  sleep "$HEALTH_GRACE"
  if service_is_healthy "$unit" "$health"; then
    SUMMARY+=("$name: updated, healthy")
    return
  fi

  log "  $unit did not come back — rolling back"
  restore_snapshot "$pip" "$before" "$name"
  sudo systemctl restart "$unit" 2>&1 | log_raw
  sleep "$HEALTH_GRACE"
  if service_is_healthy "$unit" "$health"; then
    SUMMARY+=("$name: update rolled back, service healthy again")
  else
    SUMMARY+=("$name: DOWN after rollback — needs a look")
  fi
  FAILURES+=("$name")
}

# ─── 3. Run ─────────────────────────────────────────────────────────────────
rotate_log
log "=================================================="
log "Updater started${ONLY:+ (only $ONLY)}$($DRY_RUN && echo ' (dry run)')"

$SKIP_SYSTEM || system_update

MATCHED=false
for project in "${PROJECTS[@]}"; do
  IFS='|' read -r name dir unit restart health test_cmd <<< "$project"
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then continue; fi
  MATCHED=true
  update_project "$name" "$dir" "$unit" "$restart" "$health" "$test_cmd"
done

if [ -n "$ONLY" ] && ! $MATCHED; then
  log "No project called '$ONLY'."
  exit 2
fi

REBOOT_MSG=""
if [ -f /var/run/reboot-required ]; then
  REBOOT_MSG=" ⚠️ Reboot required."
  log "Reboot required."
fi

log "Updater finished: ${SUMMARY[*]}"

# ─── 4. Notification ────────────────────────────────────────────────────────
if [ -n "$UPDATER_NTFY" ] && ! $DRY_RUN; then
  if [ ${#FAILURES[@]} -gt 0 ]; then
    title="Updater: ${#FAILURES[@]} problem(s)"
    priority="high"
    tags="warning"
  else
    title="Updater"
    priority="default"
    tags="package"
  fi
  body=$(printf '%s\n' "${SUMMARY[@]}")
  curl -s -H "Title: $title" -H "Priority: $priority" -H "Tags: $tags" \
       -d "$body$REBOOT_MSG" "https://ntfy.sh/$UPDATER_NTFY" > /dev/null
fi

# A non-zero exit makes systemd mark the unit failed, so `systemctl status
# bot-updater` — and any OnFailure= hook later — can see something went wrong.
[ ${#FAILURES[@]} -eq 0 ]
