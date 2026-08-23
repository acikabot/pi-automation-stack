"""
Pi Bot Dashboard — FastAPI backend.

Run:
    uvicorn main:app --host 0.0.0.0 --port 5000

Everything the dashboard knows about the bots comes from config.py.
Adding a bot there makes it appear here automatically.
"""

import os
import json
import shutil
import secrets
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional

import psutil
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

import config

app = FastAPI(title="Pi Bot Dashboard")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TZ_OFFSET = timedelta(hours=2)

security = HTTPBasic(auto_error=False)


# ─── Auth (dormant until AUTH_ENABLED is flipped) ─────────────────────────────

def auth(credentials: Optional[HTTPBasicCredentials] = Depends(security)):
    if not config.AUTH_ENABLED:
        return True
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, config.AUTH_USER)
    ok_pass = secrets.compare_digest(credentials.password, config.AUTH_PASSWORD)
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_bot(bot_id: str) -> dict:
    for b in config.BOTS:
        if b["id"] == bot_id:
            return b
    raise HTTPException(status_code=404, detail=f"No bot with id '{bot_id}'")


def run_cmd(argv: list, timeout: int = 15) -> tuple:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {argv[0]}"


def unit_state(unit: str) -> dict:
    """Query a systemd unit. Returns active state, sub state, and enabled flag."""
    rc, out, _ = run_cmd([
        "systemctl", "show", unit,
        "--property=ActiveState,SubState,UnitFileState,ExecMainStartTimestamp",
        "--no-pager",
    ])
    props = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k] = v
    return {
        "active":     props.get("ActiveState", "unknown"),
        "sub":        props.get("SubState", ""),
        "enabled":    props.get("UnitFileState", ""),
        "since":      props.get("ExecMainStartTimestamp", ""),
    }


def to_local(dt: datetime) -> str:
    return (dt + TZ_OFFSET).strftime("%d %b %H:%M")


def tail_file(path: str, lines: int) -> str:
    if not os.path.exists(path):
        return ""
    rc, out, _ = run_cmd(["tail", "-n", str(lines), path], timeout=10)
    return out


def last_log_activity(path: str) -> Optional[str]:
    """Most recent line's timestamp, if the log is timestamped."""
    text = tail_file(path, 40)
    for line in reversed(text.splitlines()):
        parts = line.split()
        if len(parts) >= 2 and parts[0].count("-") == 2 and parts[1].count(":") == 2:
            return f"{parts[0]} {parts[1]}"
    return None


# ─── Models ───────────────────────────────────────────────────────────────────

class ServiceAction(BaseModel):
    action: str          # start | stop | restart


class RunRequest(BaseModel):
    mode: str = ""       # "" | test | morning | evening


class ChannelIn(BaseModel):
    id: str
    name: str
    niche: str


class SystemAction(BaseModel):
    action: str


class RecipientsIn(BaseModel):
    bot_id: str
    recipients: str


# ─── Bot status ───────────────────────────────────────────────────────────────

@app.get("/api/bots")
def list_bots(_=Depends(auth)):
    out = []
    for b in config.BOTS:
        svc = unit_state(f"{b['service']}.service")
        tmr = unit_state(f"{b['timer']}.timer") if b["timer"] else None
        log_path = os.path.join(b["dir"], b["log"])

        if b["kind"] == "resident":
            healthy = svc["active"] == "active"
            label = "Running" if healthy else ("Stopped" if svc["active"] == "inactive" else "Failed")
        else:
            healthy = bool(tmr and tmr["active"] == "active")
            if svc["active"] == "activating":
                label = "Running now"
            elif healthy:
                label = "Scheduled"
            else:
                label = "Timer off"

        # A oneshot that failed its last run should surface as failed.
        if b["kind"] == "scheduled" and svc["active"] == "failed":
            label, healthy = "Last run failed", False

        out.append({
            "id":       b["id"],
            "name":     b["name"],
            "blurb":    b["blurb"],
            "kind":     b["kind"],
            "service":  b["service"],
            "label":    label,
            "healthy":  healthy,
            "busy":     svc["active"] == "activating" or svc["sub"] == "start",
            "enabled":  svc["enabled"],
            "runs":     [{"label": l, "mode": m} for l, m in b["runs"]],
            "has_config": bool(b["config"]),
            "log_size": os.path.getsize(log_path) if os.path.exists(log_path) else 0,
            "last_activity": last_log_activity(log_path),
        })
    return out


@app.post("/api/bots/{bot_id}/service")
def control_service(bot_id: str, body: ServiceAction, _=Depends(auth)):
    bot = get_bot(bot_id)
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Action must be start, stop, or restart")

    unit = f"{bot['timer']}.timer" if bot["kind"] == "scheduled" else f"{bot['service']}.service"
    rc, out, err = run_cmd(["sudo", "systemctl", body.action, unit], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or "systemctl failed")
    return {"ok": True, "message": f"{bot['name']} {body.action}ed"}


@app.post("/api/bots/{bot_id}/run")
def run_bot(bot_id: str, body: RunRequest, _=Depends(auth)):
    """Fire a manual run. Detached — watch the log for progress."""
    bot = get_bot(bot_id)
    valid = [m for _, m in bot["runs"]]
    if body.mode not in valid:
        raise HTTPException(status_code=400, detail=f"Mode must be one of: {valid}")

    python = os.path.join(bot["dir"], "venv", "bin", "python")
    if not os.path.exists(python):
        raise HTTPException(status_code=500, detail="Virtual environment not found for this bot")

    argv = [python, bot["script"]]
    if body.mode:
        argv.append(body.mode)

    log_path = os.path.join(bot["dir"], bot["log"])
    logfile = open(log_path, "a")
    subprocess.Popen(
        argv, cwd=bot["dir"], stdout=logfile, stderr=logfile,
        start_new_session=True,
    )
    label = next(l for l, m in bot["runs"] if m == body.mode)
    return {"ok": True, "message": f"{label} started — watch the log for output"}


# ─── Logs ─────────────────────────────────────────────────────────────────────

@app.get("/api/bots/{bot_id}/log")
def get_log(bot_id: str, lines: int = 200, _=Depends(auth)):
    bot = get_bot(bot_id)
    path = os.path.join(bot["dir"], bot["log"])
    return {"path": path, "content": tail_file(path, min(lines, 2000))}


@app.post("/api/bots/{bot_id}/log/clear")
def clear_log(bot_id: str, _=Depends(auth)):
    bot = get_bot(bot_id)
    path = os.path.join(bot["dir"], bot["log"])
    open(path, "w").close()
    return {"ok": True, "message": f"{bot['name']} log cleared"}


# ─── Channels (content bot) ───────────────────────────────────────────────────

@app.get("/api/channels")
def get_channels(_=Depends(auth)):
    bot = next((b for b in config.BOTS if b["config"]), None)
    if not bot or not os.path.exists(bot["config"]):
        return {"channels": []}
    with open(bot["config"]) as f:
        return json.load(f)


def _write_channels(data: dict):
    bot = next((b for b in config.BOTS if b["config"]), None)
    if not bot:
        raise HTTPException(status_code=404, detail="No bot has an editable channel list")
    shutil.copyfile(bot["config"], bot["config"] + ".bak")
    with open(bot["config"], "w") as f:
        json.dump(data, f, indent=2)


@app.post("/api/channels")
def add_channel(ch: ChannelIn, _=Depends(auth)):
    data = get_channels()
    if any(c["id"] == ch.id for c in data.get("channels", [])):
        raise HTTPException(status_code=400, detail="That channel ID is already in the list")
    data.setdefault("channels", []).append(ch.model_dump())
    _write_channels(data)
    return {"ok": True, "message": f"Added {ch.name}"}


@app.delete("/api/channels/{channel_id}")
def remove_channel(channel_id: str, _=Depends(auth)):
    data = get_channels()
    before = len(data.get("channels", []))
    data["channels"] = [c for c in data.get("channels", []) if c["id"] != channel_id]
    if len(data["channels"]) == before:
        raise HTTPException(status_code=404, detail="Channel not found")
    _write_channels(data)
    return {"ok": True, "message": "Channel removed"}


# ─── Email recipients ─────────────────────────────────────────────────────────

@app.get("/api/recipients")
def get_recipients(_=Depends(auth)):
    out = []
    for b in config.BOTS:
        env_path = os.path.join(b["dir"], ".env")
        value = ""
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith("EMAIL_RECIPIENT="):
                        value = line.split("=", 1)[1].strip()
                        break
        out.append({"bot_id": b["id"], "name": b["name"], "recipients": value})
    return out


@app.post("/api/recipients")
def set_recipients(body: RecipientsIn, _=Depends(auth)):
    bot = get_bot(body.bot_id)
    env_path = os.path.join(bot["dir"], ".env")
    if not os.path.exists(env_path):
        raise HTTPException(status_code=404, detail=".env not found for this bot")

    with open(env_path) as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith("EMAIL_RECIPIENT="):
            lines[i] = f"EMAIL_RECIPIENT={body.recipients}\n"
            found = True
            break
    if not found:
        lines.append(f"EMAIL_RECIPIENT={body.recipients}\n")

    with open(env_path, "w") as f:
        f.writelines(lines)

    note = " Restart the bot for this to take effect." if bot["kind"] == "resident" else ""
    return {"ok": True, "message": f"Recipients saved for {bot['name']}.{note}"}


# ─── System ───────────────────────────────────────────────────────────────────

def cpu_temp() -> Optional[float]:
    path = "/sys/class/thermal/thermal_zone0/temp"
    if os.path.exists(path):
        try:
            with open(path) as f:
                return round(int(f.read().strip()) / 1000, 1)
        except Exception:
            pass
    return None


@app.get("/api/system")
def system_stats(_=Depends(auth)):
    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    up   = datetime.now(timezone.utc) - boot

    days, rem = divmod(int(up.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    uptime = f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"

    load1, load5, load15 = os.getloadavg()

    return {
        "cpu_percent":  psutil.cpu_percent(interval=0.4),
        "cpu_temp":     cpu_temp(),
        "load":         [round(load1, 2), round(load5, 2), round(load15, 2)],
        "mem_used":     round(mem.used / 1024**3, 2),
        "mem_total":    round(mem.total / 1024**3, 2),
        "mem_percent":  mem.percent,
        "disk_used":    round(disk.used / 1024**3, 1),
        "disk_total":   round(disk.total / 1024**3, 1),
        "disk_percent": disk.percent,
        "uptime":       uptime,
        "booted":       to_local(boot),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
        "server_time":  to_local(datetime.now(timezone.utc)),
    }


@app.get("/api/schedule")
def schedule(_=Depends(auth)):
    """Next scheduled run for every timer the dashboard knows about."""
    rc, out, _err = run_cmd(
        ["systemctl", "list-timers", "--all", "--no-pager", "--output=json"], timeout=15
    )
    rows = []
    try:
        rows = json.loads(out) if out else []
    except json.JSONDecodeError:
        pass

    known = {f"{b['timer']}.timer": b["name"] for b in config.BOTS if b["timer"]}
    known[f"{config.UPDATER['timer']}.timer"] = "Dependency updater"

    result = []
    for r in rows:
        unit = r.get("unit", "")
        if unit in known:
            result.append({
                "name":  known[unit],
                "unit":  unit,
                "next":  r.get("next") or "—",
                "left":  r.get("left") or "—",
                "last":  r.get("last") or "—",
                "passed": r.get("passed") or "—",
            })
    return result


@app.get("/api/updater/log")
def updater_log(lines: int = 100, _=Depends(auth)):
    return {"content": tail_file(config.UPDATER["log"], min(lines, 1000))}


@app.post("/api/system/action")
def system_action(body: SystemAction, _=Depends(auth)):
    # Bulk bot controls are built from config so they never drift out of sync.
    if body.action in ("start-all", "stop-all", "restart-all"):
        verb = body.action.split("-")[0]
        errors = []
        for b in config.BOTS:
            unit = f"{b['timer']}.timer" if b["kind"] == "scheduled" else f"{b['service']}.service"
            rc, _o, err = run_cmd(["sudo", "systemctl", verb, unit], timeout=30)
            if rc != 0:
                errors.append(f"{b['name']}: {err or 'failed'}")
        if errors:
            raise HTTPException(status_code=500, detail="; ".join(errors))
        return {"ok": True, "message": f"All bots {verb}ed"}

    if body.action == "clear-all-logs":
        for b in config.BOTS:
            path = os.path.join(b["dir"], b["log"])
            if os.path.exists(path):
                open(path, "w").close()
        return {"ok": True, "message": "All bot logs cleared"}

    argv = config.SYSTEM_ACTIONS.get(body.action)
    if not argv:
        raise HTTPException(status_code=400, detail="Unknown action")

    rc, out, err = run_cmd(argv, timeout=30)
    if rc != 0 and body.action not in ("reboot", "shutdown"):
        raise HTTPException(status_code=500, detail=err or "Command failed")

    messages = {
        "reboot":     "Reboot started — the dashboard will drop out shortly",
        "shutdown":   "Shutdown started — the Pi is powering off",
        "update-now": "Updater started — watch the log on this page",
    }
    return {"ok": True, "message": messages.get(body.action, "Done")}


# ─── Static ───────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
