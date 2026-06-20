import os
import sys
import json
import time
import random
import threading
from pathlib import Path
from datetime import datetime

import discord
from discord.ext import tasks
from flask import Flask, request, jsonify, render_template, abort

BASE    = Path(__file__).resolve().parent
MSG_DIR = BASE / "messages"
CONFIG  = BASE / "config.json"
STATUS  = BASE / "status.json"
MSG_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "channel_id": 550390090665295892,
    "active_file": None,
    "delay_min": 6.0,
    "delay_max": 7.0,
    "loop": True,
    "running": False,
    "feed": True,
}


def read_config() -> dict:
    with _lock:
        if not CONFIG.exists():
            CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
            return dict(DEFAULT_CONFIG)
        cfg = json.loads(CONFIG.read_text())
    return {**DEFAULT_CONFIG, **cfg}


def write_config(cfg: dict) -> None:
    with _lock:
        CONFIG.write_text(json.dumps(cfg, indent=2))


def write_status(status: dict) -> None:
    with _lock:
        STATUS.write_text(json.dumps(status, indent=2))


def read_status() -> dict:
    with _lock:
        if not STATUS.exists():
            return {}
        return json.loads(STATUS.read_text())


def safe_name(name: str) -> str:
    name = os.path.basename(name.strip())
    if not name or name in (".", ".."):
        abort(400, "bad filename")
    if not name.endswith(".txt"):
        name += ".txt"
    return name


def load_lines(filename: str) -> list:
    path = MSG_DIR / filename
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln]


bot = discord.Client()

_run = {
    "messages": [],
    "index": 0,
    "next_at": 0.0,
    "was_running": False,
    "active_file": None,
    "feed": [],
    "error": None,
}
FEED_CAP = 40


def push_status():
    cfg = read_config()
    write_status({
        "connected": bot.is_ready(),
        "bot_name": str(bot.user) if bot.user else None,
        "running": cfg["running"],
        "active_file": _run["active_file"],
        "index": _run["index"],
        "total": len(_run["messages"]),
        "error": _run["error"],
        "feed": _run["feed"][-FEED_CAP:] if cfg["feed"] else [],
        "updated": datetime.now().strftime("%H:%M:%S"),
    })


@bot.event
async def on_ready():
    print(f"Connected as {bot.user} ({bot.user.id})")
    if not poster.is_running():
        poster.start()
    push_status()


@tasks.loop(seconds=1.0)
async def poster():
    cfg = read_config()
    now = time.monotonic()

    if not cfg["running"]:
        if _run["was_running"]:
            _run["was_running"] = False
            push_status()
        return

    fresh_start  = not _run["was_running"]
    file_changed = cfg["active_file"] != _run["active_file"]
    if fresh_start or file_changed:
        _run["active_file"] = cfg["active_file"]
        _run["messages"]    = load_lines(cfg["active_file"]) if cfg["active_file"] else []
        _run["index"]       = 0
        _run["next_at"]     = now
        _run["was_running"] = True
        _run["error"]       = None
        if not _run["messages"]:
            _run["error"] = "active file is empty or unset"
            cfg["running"] = False
            write_config(cfg)
            push_status()
            return

    if now < _run["next_at"]:
        return

    if _run["index"] >= len(_run["messages"]):
        if cfg["loop"]:
            _run["index"] = 0
        else:
            cfg["running"] = False
            write_config(cfg)
            _run["was_running"] = False
            push_status()
            return

    channel = bot.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
    if channel is None and cfg["channel_id"]:
        try:
            channel = await bot.fetch_channel(cfg["channel_id"])
        except Exception:
            channel = None
    if channel is None:
        _run["error"] = "channel not found"
        cfg["running"] = False
        write_config(cfg)
        push_status()
        return

    line = _run["messages"][_run["index"]]
    try:
        await channel.send(line)
        _run["error"] = None
        if cfg["feed"]:
            _run["feed"].append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "text": line[:200],
            })
            _run["feed"] = _run["feed"][-FEED_CAP:]
    except discord.Forbidden:
        _run["error"] = "missing permission to send in that channel"
        cfg["running"] = False
        write_config(cfg)
        push_status()
        return
    except discord.HTTPException as e:
        _run["error"] = f"send failed: {e}"
        _run["next_at"] = now + 10
        push_status()
        return

    _run["index"]   += 1
    _run["next_at"]  = now + random.uniform(cfg["delay_min"], cfg["delay_max"])
    push_status()


@poster.before_loop
async def _before():
    await bot.wait_until_ready()


app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/ping")
def ping():
    return "pong", 200


@app.get("/api/files")
def api_files():
    files = sorted(p.name for p in MSG_DIR.glob("*.txt"))
    return jsonify({
        "files": [{"name": n, "lines": len(load_lines(n))} for n in files]
    })


@app.get("/api/file/<name>")
def api_file_get(name):
    name = safe_name(name)
    path = MSG_DIR / name
    if not path.exists():
        abort(404)
    return jsonify({"name": name, "content": path.read_text(encoding="utf-8")})


@app.post("/api/file/<name>")
def api_file_save(name):
    name = safe_name(name)
    content = (request.json or {}).get("content", "")
    (MSG_DIR / name).write_text(content, encoding="utf-8")
    return jsonify({"ok": True, "name": name, "lines": len(load_lines(name))})


@app.delete("/api/file/<name>")
def api_file_delete(name):
    name = safe_name(name)
    path = MSG_DIR / name
    if path.exists():
        path.unlink()
    cfg = read_config()
    if cfg["active_file"] == name:
        cfg["active_file"] = None
        cfg["running"] = False
        write_config(cfg)
    return jsonify({"ok": True})


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    name = safe_name(f.filename)
    (MSG_DIR / name).write_text(f.read().decode("utf-8", "replace"), encoding="utf-8")
    return jsonify({"ok": True, "name": name, "lines": len(load_lines(name))})


@app.get("/api/config")
def api_config_get():
    return jsonify(read_config())


@app.post("/api/config")
def api_config_set():
    cfg = read_config()
    data = request.json or {}
    if "channel_id" in data:
        raw = str(data["channel_id"]).strip()
        cfg["channel_id"] = int(raw) if raw.isdigit() else None
    if "active_file" in data:
        cfg["active_file"] = safe_name(data["active_file"]) if data["active_file"] else None
    for key in ("delay_min", "delay_max"):
        if key in data:
            cfg[key] = max(1.0, float(data[key]))
    if cfg["delay_max"] < cfg["delay_min"]:
        cfg["delay_max"] = cfg["delay_min"]
    for key in ("loop", "feed"):
        if key in data:
            cfg[key] = bool(data[key])
    write_config(cfg)
    return jsonify(cfg)


@app.post("/api/control")
def api_control():
    action = (request.json or {}).get("action")
    cfg = read_config()
    if action == "start":
        if not cfg["active_file"]:
            return jsonify({"ok": False, "error": "pick an active file first"}), 400
        if not cfg["channel_id"]:
            return jsonify({"ok": False, "error": "set a channel ID first"}), 400
        cfg["running"] = True
    elif action == "stop":
        cfg["running"] = False
    else:
        abort(400, "unknown action")
    write_config(cfg)
    return jsonify({"ok": True, "running": cfg["running"]})


@app.get("/api/status")
def api_status():
    return jsonify(read_status())


def run_flask():
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit("Set DISCORD_TOKEN env var.")

    if not CONFIG.exists():
        write_config(dict(DEFAULT_CONFIG))

    threading.Thread(target=run_flask, daemon=True).start()
    print(f"Dashboard: http://0.0.0.0:{os.environ.get('PORT', '5000')}")
    bot.run(token)


if __name__ == "__main__":
    main()
