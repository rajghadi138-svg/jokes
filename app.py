#!/usr/bin/env python3
"""
Dispatcher — Discord self-bot auto-poster with a web control panel.

⚠️  WARNING: Self-bots violate Discord's Terms of Service.
    Using your user token can result in a permanent account ban.
    Only use this on servers you own, and understand the risks.

A self-bot that posts lines from a text file into a configured channel,
paced by a random delay. Everything is controlled from a local web dashboard:
upload / edit / create message files, pick which file is active, set the
channel and delay, arm / disarm posting, toggle the feed.

Setup:
    python -m venv .venv
    source .venv/bin/activate.fish  # or .venv/bin/activate
    pip install -U "discord.py>=2.3" flask

    # Get your user token (BROWSER METHOD - use at your own risk):
    # 1. Open Discord in browser
    # 2. Press F12 → Application → Local Storage → discord.com
    # 3. Copy the "token" value
    # set -x DISCORD_TOKEN "your_user_token"

Run:
    python app.py
    # open http://127.0.0.1:5000
"""
import os
import sys
import json
import time
import random
import threading
from pathlib import Path
from datetime import datetime

import discord
from discord.ext import commands, tasks
from flask import Flask, request, jsonify, render_template, abort

# ---- paths --------------------------------------------------------------
BASE      = Path(__file__).resolve().parent
MSG_DIR   = BASE / "messages"
CONFIG    = BASE / "config.json"
STATUS    = BASE / "status.json"
MSG_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "channel_id": None,
    "active_file": None,
    "delay_min": 6.0,
    "delay_max": 7.0,
    "loop": True,
    "running": False,
    "feed": True,
}


# ---- state helpers ------------------------------------------------------
def read_config() -> dict:
    with _lock:
        if not CONFIG.exists():
            CONFIG.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
            return dict(DEFAULT_CONFIG)
        cfg = json.loads(CONFIG.read_text())
    merged = {**DEFAULT_CONFIG, **cfg}
    return merged


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


def load_lines(filename: str) -> list[str]:
    path = MSG_DIR / filename
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    return [ln for ln in lines if ln]


# =========================================================================
#  Self-Bot Cog
# =========================================================================
class DispatcherCog(commands.Cog):
    """
    Self-bot cog that handles the auto-poster loop.
    """
    FEED_CAP = 40

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._messages: list[str] = []
        self._index: int = 0
        self._next_at: float = 0.0
        self._was_running: bool = False
        self._active_file: str | None = None
        self._feed: list[dict] = []
        self._error: str | None = None

        self.poster.start()

    def cog_unload(self):
        self.poster.cancel()

    def push_status(self):
        cfg = read_config()
        write_status({
            "connected": self.bot.is_ready(),
            "bot_name": str(self.bot.user) if self.bot.user else None,
            "running": cfg["running"],
            "active_file": self._active_file,
            "index": self._index,
            "total": len(self._messages),
            "error": self._error,
            "feed": self._feed[-self.FEED_CAP:] if cfg["feed"] else [],
            "updated": datetime.now().strftime("%H:%M:%S"),
        })

    @tasks.loop(seconds=1.0)
    async def poster(self):
        cfg = read_config()
        now = time.monotonic()

        if not cfg["running"]:
            if self._was_running:
                self._was_running = False
                self.push_status()
            return

        fresh_start = not self._was_running
        file_changed = cfg["active_file"] != self._active_file
        if fresh_start or file_changed:
            self._active_file = cfg["active_file"]
            self._messages = load_lines(cfg["active_file"]) if cfg["active_file"] else []
            self._index = 0
            self._next_at = now
            self._was_running = True
            self._error = None
            if not self._messages:
                self._error = "active file is empty or unset — nothing to post"
                cfg["running"] = False
                write_config(cfg)
                self.push_status()
                return

        if now < self._next_at:
            return

        if self._index >= len(self._messages):
            if cfg["loop"]:
                self._index = 0
            else:
                cfg["running"] = False
                write_config(cfg)
                self._was_running = False
                self.push_status()
                return

        channel = self.bot.get_channel(cfg["channel_id"]) if cfg["channel_id"] else None
        if channel is None:
            self._error = "channel not found — check the channel ID / bot access"
            cfg["running"] = False
            write_config(cfg)
            self.push_status()
            return

        line = self._messages[self._index]
        try:
            await channel.send(line)
            self._error = None
            if cfg["feed"]:
                self._feed.append({
                    "t": datetime.now().strftime("%H:%M:%S"),
                    "text": line[:200],
                })
                self._feed = self._feed[-self.FEED_CAP:]
        except discord.Forbidden:
            self._error = "missing permission to send in that channel"
            cfg["running"] = False
            write_config(cfg)
            self.push_status()
            return
        except discord.HTTPException as e:
            self._error = f"send failed (rate-limited?): {e}"
            self._next_at = now + 10
            self.push_status()
            return

        self._index += 1
        self._next_at = now + random.uniform(cfg["delay_min"], cfg["delay_max"])
        self.push_status()

    @poster.before_loop
    async def before_poster(self):
        await self.bot.wait_until_ready()

    # Optional: self-bot commands (prefix-based, no slash commands for self-bots)
    @commands.command(name="status")
    async def status_cmd(self, ctx: commands.Context):
        """Show dispatcher status."""
        if ctx.author != self.bot.user:
            return
        cfg = read_config()
        embed = discord.Embed(title="Dispatcher Status", color=0x3498db)
        embed.add_field(name="Running", value="✅ Yes" if cfg["running"] else "❌ No", inline=True)
        embed.add_field(name="Active File", value=self._active_file or "None", inline=True)
        embed.add_field(name="Progress", value=f"{self._index}/{len(self._messages)}", inline=True)
        embed.add_field(name="Channel", value=f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "None", inline=True)
        if self._error:
            embed.add_field(name="Error", value=self._error, inline=False)
        await ctx.send(embed=embed, delete_after=10)

    @commands.command(name="arm")
    async def arm_cmd(self, ctx: commands.Context):
        """Arm the poster."""
        if ctx.author != self.bot.user:
            return
        cfg = read_config()
        if not cfg["active_file"]:
            await ctx.send("❌ No active file set.", delete_after=5)
            return
        if not cfg["channel_id"]:
            await ctx.send("❌ No channel ID set.", delete_after=5)
            return
        cfg["running"] = True
        write_config(cfg)
        await ctx.send("✅ Poster armed.", delete_after=5)

    @commands.command(name="disarm")
    async def disarm_cmd(self, ctx: commands.Context):
        """Disarm the poster."""
        if ctx.author != self.bot.user:
            return
        cfg = read_config()
        cfg["running"] = False
        write_config(cfg)
        await ctx.send("✅ Poster disarmed.", delete_after=5)

    @commands.command(name="skip")
    async def skip_cmd(self, ctx: commands.Context):
        """Skip to the next line."""
        if ctx.author != self.bot.user:
            return
        if self._index < len(self._messages) - 1:
            self._index += 1
            self._next_at = time.monotonic()
            self.push_status()
            await ctx.send(f"⏭️ Skipped. Next: `{self._messages[self._index][:100]}`", delete_after=5)
        else:
            await ctx.send("⚠️ Already at last line.", delete_after=5)


# =========================================================================
#  Self-Bot class (uses user token)
# =========================================================================
class SelfBot(commands.Bot):
    def __init__(self):
        # Self-bots don't need most intents, but we need minimal ones
        intents = discord.Intents.default()
        # No message_content needed for just sending, but enable if you want
        # to read your own commands
        # intents.message_content = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            self_bot=True,  # <-- KEY: enables self-bot mode
        )

    async def setup_hook(self):
        await self.add_cog(DispatcherCog(self))
        print(f"Self-bot loaded. Logged in as {self.user} ({self.user.id})")

    async def on_ready(self):
        print(f"Ready! {self.user} is online.")


# =========================================================================
#  Flask dashboard
# =========================================================================
app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


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
    content = request.json.get("content", "")
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


# =========================================================================
#  Boot
# =========================================================================
def run_flask():
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit('Set DISCORD_TOKEN.  fish:  set -x DISCORD_TOKEN "your_user_token"')

    if not CONFIG.exists():
        write_config(dict(DEFAULT_CONFIG))

    threading.Thread(target=run_flask, daemon=True).start()
    print("Dashboard: http://127.0.0.1:5000")

    bot = SelfBot()
    bot.run(token)


if __name__ == "__main__":
    main()
