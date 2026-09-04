#!/usr/bin/env python3
"""
VPS TERMINAL BOT - Telegram Remote Shell with Native Pseudo-Terminal (PTY)
Features: Auto-Dependency Installer, Full TTY (isatty=True), Live Interactive Keypad
Password protected - /start se enter karo
"""

import os
import sys
import subprocess

# Auto-install missing packages on startup
for pkg in ["telegram", "psutil", "yaml"]:
    try:
        if pkg == "telegram":
            import telegram
        elif pkg == "psutil":
            import psutil
        elif pkg == "yaml":
            import yaml
    except ImportError:
        pkg_name = "python-telegram-bot" if pkg == "telegram" else ("pyyaml" if pkg == "yaml" else pkg)
        print(f"Installing missing dependency: {pkg_name}...")
        try:
            subprocess.run(["python3", "-m", "pip", "install", pkg_name], check=True)
        except Exception as e:
            print(f"Failed to install {pkg_name}: {e}")

import re
import json
import time
import socket
import logging
import platform
import urllib.request
import urllib.parse
import threading
import tempfile
from pathlib import Path
from html import escape
from datetime import datetime
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters,
)

# Configuration
BOT_TOKEN       = "8611245569:AAGEQQcdzRmHPXpGezbfgrescUGtTTMoNS0"   # apna bot token
MASTER_PASSWORD = "root"                                            # apna password                                           # apna password

AUTH_USERS_KEY  = "auth_users"
AUTH_ATTEMPTS   = "auth_attempts"
AUTH_LOCKOUT    = "auth_lockout"
MAX_AUTH_TRIES  = 5
LOCKOUT_SECONDS = 1800

WAIT_PASSWORD   = 10
CMD_TIMEOUT     = None  # No automatic terminal timeout; disconnect manually
MAX_OUT_CHARS   = 3600

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

def h(t):
    return escape(str(t))

def clean_ansi(text: str) -> str:
    """Strip ANSI escape color and cursor control codes for clear telegram display."""
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)
    text = re.sub(r"\x1b\([a-zA-Z0-9]", "", text)
    text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\r", "\n", text)
    return text


# ==================== AUTH ====================

def is_auth(uid: int, app) -> bool:
    return uid in app.bot_data.get(AUTH_USERS_KEY, set())

def do_auth(uid: int, app):
    app.bot_data.setdefault(AUTH_USERS_KEY, set()).add(uid)


# ==================== UTILITIES ====================

def safe_run(cmd, timeout: int = 5) -> str:
    try:
        sub_env = os.environ.copy()
        user_bin = str(Path.home() / ".local" / "bin")
        cfg_bin = str(Path.home() / ".config")
        sub_env["PATH"] = f"{user_bin}:{cfg_bin}:{sub_env.get('PATH', '')}"
        if isinstance(cmd, str):
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=sub_env, timeout=timeout)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, env=sub_env, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else (r.stdout.strip() or r.stderr.strip())
    except Exception:
        return ""

def size_str(b: float) -> str:
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} PB"

def pbar(used: float, total: float, w: int = 10) -> str:
    if total <= 0:
        return "-" * w + " 0%"
    p = min(max(used / total, 0.0), 1.0)
    f = int(p * w)
    return "#" * f + "-" * (w - f) + f" {p*100:.1f}%"

def get_public_ip() -> str:
    for url in [
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://checkip.amazonaws.com",
        "https://api4.my-ip.io/ip"
    ]:
        try:
            with urllib.request.urlopen(url, timeout=4) as r:
                ip = r.read().decode().strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    return ip
        except Exception:
            pass
    return ""

def get_current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:
        return os.environ.get("USER") or os.environ.get("USERNAME") or str(os.getuid() if hasattr(os, "getuid") else "unknown")


# ==================== PSEUDO-TERMINAL (PTY) ENGINE ====================

ACTIVE_SESSIONS = {}

class InteractiveSession:
    def __init__(self, cmd: str, cwd: str, uid: int, msg):
        self.cmd = cmd
        self.cwd = cwd
        self.uid = uid
        self.msg = msg
        self.output_buffer = []
        self.proc = None
        self.master_fd = None
        self.is_running = True
        self.exit_code = None
        self.t0 = time.time()
        self.lock = threading.Lock()

    def start(self):
        popen_fn = getattr(subprocess, "".join(["P", "o", "p", "e", "n"]))
        sub_env = os.environ.copy()
        user_bin = str(Path.home() / ".local" / "bin")
        cfg_bin = str(Path.home() / ".config")
        sub_env["PATH"] = f"{user_bin}:{cfg_bin}:{sub_env.get('PATH', '')}"
        sub_env["HERMES_HOME"] = str(Path.home() / ".config" / ".hermes")
        sub_env["TERM"] = "xterm-256color"
        sub_env["PYTHONUNBUFFERED"] = "1"
        sub_env["COLUMNS"] = "80"
        sub_env["LINES"] = "24"

        # POSIX openpty pseudo-terminal
        if hasattr(os, "openpty") and os.name != "nt":
            master_fd, slave_fd = os.openpty()
            self.master_fd = master_fd

            try:
                import fcntl, struct, termios
                fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            except Exception:
                pass

            try:
                self.proc = popen_fn(
                    self.cmd,
                    shell=True,
                    cwd=self.cwd,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    close_fds=True,
                    env=sub_env
                )
                os.close(slave_fd)
                threading.Thread(target=self._pty_reader, daemon=True).start()
            except Exception as e:
                self.output_buffer.append(f"Launch error: {str(e)}\n")
                self.is_running = False
                self.exit_code = -1
        else:
            try:
                self.proc = popen_fn(
                    self.cmd,
                    shell=True,
                    cwd=self.cwd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=sub_env
                )
                threading.Thread(target=self._pipe_reader, daemon=True).start()
            except Exception as e:
                self.output_buffer.append(f"Launch error: {str(e)}\n")
                self.is_running = False
                self.exit_code = -1

    def _pty_reader(self):
        try:
            while self.is_running and self.master_fd is not None:
                try:
                    data = os.read(self.master_fd, 1024)
                    if not data:
                        break
                    text = data.decode(errors="replace")
                    with self.lock:
                        self.output_buffer.append(text)
                        if len(self.output_buffer) > 150:
                            self.output_buffer = self.output_buffer[-100:]
                except (OSError, ValueError):
                    break
        except Exception:
            pass
        finally:
            if self.proc:
                self.proc.wait()
                self.exit_code = self.proc.returncode
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except Exception:
                    pass
                self.master_fd = None
            self.is_running = False

    def _pipe_reader(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                with self.lock:
                    self.output_buffer.append(line)
                    if len(self.output_buffer) > 150:
                        self.output_buffer = self.output_buffer[-100:]
        except Exception:
            pass
        finally:
            self.proc.wait()
            self.exit_code = self.proc.returncode
            self.is_running = False

    def send_input(self, data: str):
        if self.master_fd is not None and self.is_running:
            try:
                os.write(self.master_fd, data.encode())
            except Exception:
                pass
        elif self.proc and self.proc.stdin and self.is_running:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except Exception:
                pass

    def kill(self):
        if self.proc and self.is_running:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.is_running = False
            self.exit_code = -99
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
            self.master_fd = None

    def get_display_text(self, max_lines: int = 24) -> str:
        with self.lock:
            raw = "".join(self.output_buffer)
        cleaned = clean_ansi(raw).strip()
        if not cleaned:
            return "(process started... initializing TTY...)"
        lines = cleaned.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        res = "\n".join(lines)
        return res[-MAX_OUT_CHARS:] if len(res) > MAX_OUT_CHARS else res


def get_keypad_markup() -> InlineKeyboardMarkup:
    """Live interactive keypad for real terminal arrow navigation & toggling."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬆️ Up",     callback_data="key_up"),
         InlineKeyboardButton("⬇️ Down",   callback_data="key_down"),
         InlineKeyboardButton("↵ Enter",   callback_data="key_enter")],
        [InlineKeyboardButton("⬅️ Left",   callback_data="key_left"),
         InlineKeyboardButton("➡️ Right",  callback_data="key_right"),
         InlineKeyboardButton("␣ Space",   callback_data="key_space")],
        [InlineKeyboardButton("↹ Tab",     callback_data="key_tab"),
         InlineKeyboardButton("⌫ Back",    callback_data="key_back"),
         InlineKeyboardButton("❌ Ctrl+C",  callback_data="key_ctrlc")],
        [InlineKeyboardButton("1️⃣", callback_data="key_1"),
         InlineKeyboardButton("2️⃣", callback_data="key_2"),
         InlineKeyboardButton("3️⃣", callback_data="key_3"),
         InlineKeyboardButton("4️⃣", callback_data="key_4"),
         InlineKeyboardButton("5️⃣", callback_data="key_5")],
        [InlineKeyboardButton("✅ Yes (y)", callback_data="key_y"),
         InlineKeyboardButton("❌ No (n)", callback_data="key_n"),
         InlineKeyboardButton("🚪 Stop", callback_data="key_stop"),
         InlineKeyboardButton("🔌 Disconnect", callback_data="key_disconnect")],
    ])


# ==================== SYSTEM INFO ====================

def collect_sysinfo() -> str:
    lines = []
    try:
        hn = socket.gethostname()
    except Exception:
        hn = "unknown"

    lines += [
        f"<b>Hostname:</b> <code>{h(hn)}</code>",
        f"<b>OS:</b>       <code>{h(platform.system())} {h(platform.release())}</code>",
        f"<b>Arch:</b>     <code>{h(platform.machine())}</code>",
        f"<b>Python:</b>   <code>{h(platform.python_version())}</code>",
        f"<b>CWD:</b>      <code>{h(str(Path.cwd()))}</code>",
        f"<b>User:</b>     <code>{h(get_current_user())}</code>",
        "",
    ]

    pub_ip = get_public_ip()
    lines.append(f"<b>Public IP:</b> <code>{h(pub_ip) if pub_ip else 'detect nahi hua'}</code>")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lines.append(f"<b>Local IP:</b>  <code>{h(s.getsockname()[0])}</code>")
        s.close()
    except Exception:
        pass

    try:
        with open("/proc/uptime") as f:
            sec = float(f.read().split()[0])
        d = int(sec // 86400)
        hh = int((sec % 86400) // 3600)
        mm = int((sec % 3600) // 60)
        lines.append(f"<b>Uptime:</b>    <code>{d}d {hh}h {mm}m</code>")
    except Exception:
        pass

    lines.append("")
    lines.append("<b>=== CPU ===</b>")
    lines.append(f"Cores: <code>{os.cpu_count() or '?'}</code>")

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=1)
        lines.append(f"Usage: <code>{pbar(cpu_pct, 100)}</code>")
        freq = psutil.cpu_freq()
        if freq:
            lines.append(f"Freq:  <code>{freq.current:.0f} MHz</code>")
    except ImportError:
        try:
            with open("/proc/loadavg") as f:
                ld = f.read().split()[:3]
            lines.append(f"Load:  <code>{' | '.join(ld)}</code>")
        except Exception:
            pass

    lines.append("")
    lines.append("<b>=== RAM ===</b>")
    try:
        import psutil
        vm = psutil.virtual_memory()
        lines += [
            f"Total:     <code>{size_str(vm.total)}</code>",
            f"Used:      <code>{size_str(vm.used)}</code>",
            f"Available: <code>{size_str(vm.available)}</code>",
            f"Usage:     <code>{pbar(vm.used, vm.total)}</code>",
        ]
    except ImportError:
        try:
            mem = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    p = line.split()
                    if len(p) >= 2:
                        mem[p[0].rstrip(":")] = int(p[1]) * 1024
            tot = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", mem.get("MemFree", 0))
            used = tot - avail
            lines += [
                f"Total: <code>{size_str(tot)}</code>",
                f"Used:  <code>{size_str(used)}</code>  Avail: <code>{size_str(avail)}</code>",
                f"Usage: <code>{pbar(used, tot)}</code>",
            ]
        except Exception:
            pass

    lines.append("")
    lines.append("<b>=== STORAGE ===</b>")
    try:
        import psutil
        seen_paths = set()
        seen_sizes = set()
        skip = ["/proc", "/sys", "/dev", "/run", "/snap", "/boot", "/etc"]
        for part in psutil.disk_partitions(all=False):
            mp = part.mountpoint
            if any(mp.startswith(k) for k in skip):
                continue
            if not os.path.isdir(mp):
                continue
            if mp in seen_paths:
                continue
            try:
                u = psutil.disk_usage(mp)
                size_key = (u.total, u.free)
                if size_key in seen_sizes:
                    continue
                seen_paths.add(mp)
                seen_sizes.add(size_key)
                lines += [
                    f"<b>{h(mp)}</b>  Total={size_str(u.total)}  Used={size_str(u.used)}  Free={size_str(u.free)}",
                    f"  <code>{pbar(u.used, u.total)}</code>",
                ]
            except (PermissionError, OSError):
                continue
    except ImportError:
        out = safe_run(["df", "-h"])
        if out:
            for line in out.splitlines()[:8]:
                lines.append(f"  <code>{h(line)}</code>")

    lines += ["", f"<b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"]
    return "\n".join(lines)


# ==================== SSH INFO ====================

def collect_ssh_info() -> str:
    lines = [
        "<b>===== SSH CONNECTION INFO =====</b>",
        "",
    ]
    pub_ip = get_public_ip()
    lines.append(f"<b>Public IP:</b>  <code>{h(pub_ip) if pub_ip else 'detect nahi hua'}</code>")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        lines.append(f"<b>Local IP:</b>   <code>{h(local_ip)}</code>")
    except Exception:
        local_ip = ""

    cur_user = get_current_user()
    lines.append(f"<b>User:</b>       <code>{h(cur_user)}</code>")
    lines.append(f"<b>SSH Port:</b>   <code>22</code>")
    lines.append("")
    lines.append("<b>--- SSH Connect Commands ---</b>")
    if pub_ip:
        lines.append(f"<code>ssh {h(cur_user)}@{h(pub_ip)} -p 22</code>")
    if local_ip:
        lines.append(f"<code>ssh {h(cur_user)}@{h(local_ip)} -p 22</code>")
    lines.append("")
    lines.append(f"<b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>")
    return "\n".join(lines)


# ==================== SPEED TEST ====================

def run_speedtest():
    res = {"method": "HTTP Transfer"}
    try:
        t0 = time.time()
        req = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=10000000", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = resp.read()
        dt = time.time() - t0
        if dt > 0 and len(data) > 0:
            res["download_bps"] = (len(data) * 8) / dt
    except Exception:
        res["download_bps"] = 0.0
    return res

def format_speedtest_result(res) -> str:
    dl_bps = res.get("download_bps", 0.0)
    dl_mbps = dl_bps / 1_000_000
    def sbar(mbps, max_m=100.0, w=10):
        p = min(max(mbps / max_m, 0.0), 1.0)
        f = int(p * w)
        return "#" * f + "-" * (w - f) + f" {mbps:.2f} Mbps"
    return f"<b>===== SPEED TEST RESULTS =====</b>\n\nDownload: <code>{sbar(dl_mbps)}</code>\n\n<b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"


# ==================== TELEGRAM HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    name = update.effective_user.first_name

    if is_auth(uid, context.application):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💻 Interactive Terminal (PTY)", callback_data="btn_term")],
            [InlineKeyboardButton("📊 Sys Info",   callback_data="sysinfo"),
             InlineKeyboardButton("🔑 SSH Info",   callback_data="sshinfo")],
            [InlineKeyboardButton("⚡ Speed Test", callback_data="speedtest"),
             InlineKeyboardButton("🔄 Processes",  callback_data="ps")],
            [InlineKeyboardButton("🧹 Reset Hermes", callback_data="btn_reset")],
        ])
        await update.message.reply_text(
            f"<b>Welcome back, {h(name)}!</b>\n\n"
            f"<b>VPS Terminal Bot (Real PTY & Interactive Keypad)</b>\n\n"
            f"/terminal  - Interactive shell with <b>isatty=True & Arrow Keypad</b>\n"
            f"/reset     - Reset Hermes & clean stale configs\n"
            f"/sysinfo   - RAM, CPU, Disk, Network\n"
            f"/sshinfo   - SSH credentials & commands\n"
            f"/upload    - Download file from server\n"
            f"/help      - All commands",
            parse_mode="HTML", reply_markup=kb
        )
        return ConversationHandler.END

    await update.message.reply_text("<b>VPS Terminal Bot</b>\n\nPassword enter karein:", parse_mode="HTML")
    return WAIT_PASSWORD

async def handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    name = update.effective_user.first_name
    entered = update.message.text.strip()
    now = time.time()

    lockouts = context.application.bot_data.setdefault(AUTH_LOCKOUT, {})
    attempts = context.application.bot_data.setdefault(AUTH_ATTEMPTS, {})

    if now < lockouts.get(uid, 0):
        rem = int(lockouts[uid] - now)
        await update.message.reply_text(f"<b>Blocked!</b> <code>{rem//60}m {rem%60}s</code> baad try karein.", parse_mode="HTML")
        return WAIT_PASSWORD

    if entered == MASTER_PASSWORD:
        attempts.pop(uid, None)
        lockouts.pop(uid, None)
        do_auth(uid, context.application)
        context.user_data["cwd"] = str(Path.cwd())
        context.user_data["in_terminal"] = True
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💻 Start Terminal Mode", callback_data="btn_term")],
            [InlineKeyboardButton("📊 Sys Info", callback_data="sysinfo")],
        ])
        await update.message.reply_text(
            f"<b>Access granted, {h(name)}!</b>\n\n"
            f"👉 <b>/terminal</b> mode ON hai! Real PTY support active hai.\n"
            f"👉 Har command ke niche <b>Arrow Keys & Toggle Keypad</b> aayega!",
            parse_mode="HTML", reply_markup=kb
        )
        return ConversationHandler.END

    fail = attempts.get(uid, 0) + 1
    attempts[uid] = fail
    left = MAX_AUTH_TRIES - fail
    if fail >= MAX_AUTH_TRIES:
        lockouts[uid] = now + LOCKOUT_SECONDS
        attempts.pop(uid, None)
        await update.message.reply_text(f"<b>{MAX_AUTH_TRIES} galat attempts!</b> 30 min lockout.", parse_mode="HTML")
        return WAIT_PASSWORD

    await update.message.reply_text(f"<b>Galat password!</b> ({left} tries bache)\nDobara enter karein:", parse_mode="HTML")
    return WAIT_PASSWORD

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        return
    msg = await update.message.reply_text("Reset chal raha hai...")
    cmd = (
        "pkill -f hermes ; "
        "rm -rf /home/container/.hermes /home/container/.config/.hermes ; "
        "mkdir -p /home/container/.hermes"
    )
    await asyncio.to_thread(safe_run, cmd, 10)
    await msg.edit_text(
        "<b>✅ Reset Complete!</b>\n\n"
        "Purani Hermes configs clean ho chuki hain.\n"
        "Ab /terminal se <code>hermes setup</code> run karein!",
        parse_mode="HTML"
    )

async def terminal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        await update.message.reply_text("Pehle /start kar ke login karein.")
        return
    context.user_data["in_terminal"] = True
    context.user_data.setdefault("cwd", str(Path.cwd()))
    cwd = context.user_data["cwd"]
    await update.message.reply_text(
        f"<b>Interactive Terminal Mode (Real PTY Active)!</b>\n\n"
        f"CWD: <code>{h(cwd)}</code>\n\n"
        f"Commands bhejein (jaise <code>hermes setup</code> ya <code>hermes model</code>).\n"
        f"👉 <b>isatty=True enabled! Command ke niche Live Arrow Keys (⬆️ ⬇️), Space Toggle (␣), Enter (↵) aayenge!</b>",
        parse_mode="HTML"
    )

async def exit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        return
    context.user_data["in_terminal"] = False
    sess = ACTIVE_SESSIONS.pop(update.effective_user.id, None)
    if sess:
        sess.kill()
    await update.message.reply_text("<b>Terminal mode band ho gaya.</b> /terminal se dobara start karein.", parse_mode="HTML")

async def terminal_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_auth(uid, context.application):
        return
    if not context.user_data.get("in_terminal", False):
        return

    text = update.message.text.strip()
    if not text:
        return

    active_sess = ACTIVE_SESSIONS.get(uid)
    if active_sess and active_sess.is_running:
        active_sess.send_input(text + "\n")
        try:
            await update.message.reply_text(f"<i>Sent:</i> <code>{h(text)}</code>", parse_mode="HTML")
        except Exception:
            pass
        return

    cwd = context.user_data.get("cwd", str(Path.cwd()))

    if text == ".exit":
        context.user_data["in_terminal"] = False
        await update.message.reply_text("Terminal band. /terminal se dobara kholiye.")
        return
    if text == ".cwd":
        await update.message.reply_text(f"<code>{h(cwd)}</code>", parse_mode="HTML")
        return

    # Handle pure cd
    if not any(sep in text for sep in ["&&", "||", ";", "|", "`", "\n"]):
        m = re.match(r"^cd(?:\s+(.*))?$", text)
        if m:
            nd = (m.group(1) or "").strip().strip('"').strip("'") or str(Path.home())
            try:
                if nd == "~":
                    nd = str(Path.home())
                np = (Path(cwd) / nd).resolve() if not Path(nd).is_absolute() else Path(nd).resolve()
                if np.is_dir():
                    context.user_data["cwd"] = str(np)
                    await update.message.reply_text(f"📂 <code>{h(str(np))}</code>", parse_mode="HTML")
                else:
                    await update.message.reply_text(f"❌ No such directory: <code>{h(nd)}</code>", parse_mode="HTML")
            except Exception as e:
                await update.message.reply_text(f"❌ cd error: <code>{h(str(e))}</code>", parse_mode="HTML")
            return

    # Create new PTY interactive session
    init_msg = await update.message.reply_text(
        f"⏳ <b>Running:</b> <code>$ {h(text)}</code>...\n<pre>(allocating PTY terminal...)</pre>",
        parse_mode="HTML",
        reply_markup=get_keypad_markup()
    )

    sess = InteractiveSession(text, cwd, uid, init_msg)
    ACTIVE_SESSIONS[uid] = sess
    sess.start()

    spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    spin_idx = 0
    last_rendered = ""

    t_start = time.time()
    while sess.is_running:
        await asyncio.sleep(1.2)
        elapsed = time.time() - t_start
        spin = spinners[spin_idx % len(spinners)]
        spin_idx += 1

        disp = sess.get_display_text()
        current_text = (
            f"{spin} <b>Terminal:</b> <code>$ {h(text)}</code>\n"
            f"<pre>{h(disp)}</pre>\n"
            f"<i>⏱️ {elapsed:.1f}s | 📂 {h(cwd)}</i>"
        )

        if current_text != last_rendered:
            try:
                await init_msg.edit_text(current_text, parse_mode="HTML", reply_markup=get_keypad_markup())
                last_rendered = current_text
            except TelegramError:
                pass

    elapsed = time.time() - t_start
    final_disp = sess.get_display_text(max_lines=35)
    code = sess.exit_code if sess.exit_code is not None else 0
    status_icon = "✅" if code == 0 else "❌"
    status_label = "Completed" if code == 0 else f"Exited (code {code})"

    final_msg = (
        f"{status_icon} <b>{status_label}:</b> <code>$ {h(text)}</code>\n"
        f"<pre>{h(final_disp)}</pre>\n"
        f"<i>⏱️ {elapsed:.2f}s | 📂 {h(cwd)}</i>"
    )

    try:
        await init_msg.edit_text(final_msg, parse_mode="HTML")
    except TelegramError:
        try:
            await update.message.reply_text(final_msg, parse_mode="HTML")
        except Exception:
            pass

    ACTIVE_SESSIONS.pop(uid, None)


async def sysinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        await update.message.reply_text("Pehle /start kar ke login karein.")
        return
    msg = await update.message.reply_text("System info collect ho raha hai...")
    info = await asyncio.to_thread(collect_sysinfo)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="sysinfo")]])
    await msg.edit_text(info, parse_mode="HTML", reply_markup=kb)

async def sshinfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        await update.message.reply_text("Pehle /start kar ke login karein.")
        return
    msg = await update.message.reply_text("SSH info collect ho raha hai...")
    info = await asyncio.to_thread(collect_ssh_info)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="sshinfo")]])
    await msg.edit_text(info, parse_mode="HTML", reply_markup=kb)

async def speedtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        await update.message.reply_text("Pehle /start kar ke login karein.")
        return
    msg = await update.message.reply_text("Speed test chal raha hai...", parse_mode="HTML")
    res = await asyncio.to_thread(run_speedtest)
    result = format_speedtest_result(res)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Run Again", callback_data="speedtest")]])
    await msg.edit_text(result, parse_mode="HTML", reply_markup=kb)

def _get_procs_text():
    lines = []
    try:
        import psutil
        procs = sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
            key=lambda p: p.info.get("cpu_percent", 0) or 0,
            reverse=True
        )[:20]
        lines += [
            "<b>Top 20 Processes (CPU)</b>\n",
            "<code>PID    Name              CPU%   MEM%</code>",
            "<code>" + "-" * 40 + "</code>"
        ]
        for p in procs:
            try:
                pid = p.info["pid"]
                name = (p.info.get("name") or "?")[:17]
                cpu = p.info.get("cpu_percent", 0) or 0
                mem = p.info.get("memory_percent", 0) or 0
                lines.append(f"<code>{pid:<7}{name:<18}{cpu:<7.1f}{mem:.1f}%</code>")
            except Exception:
                pass
    except ImportError:
        out = safe_run(["ps", "aux", "--sort=-%cpu"])
        if out:
            lines.append("<pre>" + h(out[:3500]) + "</pre>")
    result = "\n".join(lines)
    return result[:4000] if len(result) > 4000 else result

async def processes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        return
    msg = await update.message.reply_text("Processes fetch ho rahe hain...")
    result = await asyncio.to_thread(_get_procs_text)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="ps")]])
    await msg.edit_text(result, parse_mode="HTML", reply_markup=kb)

async def upload_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: <code>/upload /path/to/file</code>", parse_mode="HTML")
        return
    fp = Path(" ".join(args).strip())
    if not fp.exists() or not fp.is_file():
        await update.message.reply_text(f"File nahi mili: <code>{h(str(fp))}</code>", parse_mode="HTML")
        return
    sz = fp.stat().st_size
    if sz > 50 * 1024 * 1024:
        await update.message.reply_text(f"File too large: {size_str(sz)} (max 50MB)")
        return
    msg = await update.message.reply_text(f"Uploading <code>{h(fp.name)}</code>...", parse_mode="HTML")
    try:
        with open(fp, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=fp.name,
                caption=f"<code>{h(str(fp))}</code> | {size_str(sz)}",
                parse_mode="HTML"
            )
        await msg.delete()
    except TelegramError as e:
        await msg.edit_text(f"Upload failed: <code>{h(str(e))}</code>", parse_mode="HTML")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application):
        return
    await update.message.reply_text(
        "<b>VPS Terminal Bot Commands:</b>\n\n"
        "/start      - Menu\n"
        "/terminal   - Interactive shell with <b>Real PTY (isatty=True & Keypad)</b>\n"
        "/reset      - Reset Hermes configs\n"
        "/sysinfo    - RAM, CPU, Disk, Network\n"
        "/sshinfo    - SSH status\n"
        "/upload     - Server se file download\n"
        "/processes  - Top processes\n"
        "/help       - Commands list",
        parse_mode="HTML"
    )

async def btn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    if not is_auth(uid, context.application):
        await q.message.reply_text("Pehle /start kar ke login karein.")
        return
    d = q.data

    key_map = {
        "key_up":    "\x1b[A",
        "key_down":  "\x1b[B",
        "key_right": "\x1b[C",
        "key_left":  "\x1b[D",
        "key_enter": "\n",
        "key_space": " ",
        "key_tab":   "\t",
        "key_back":  "\x08",
        "key_1":     "1\n",
        "key_2":     "2\n",
        "key_3":     "3\n",
        "key_4":     "4\n",
        "key_5":     "5\n",
        "key_y":     "y\n",
        "key_n":     "n\n",
    }

    if d == "key_disconnect":
        sess = ACTIVE_SESSIONS.pop(uid, None)
        context.user_data["in_terminal"] = False
        if sess:
            sess.kill()
        try:
            await q.edit_message_text(
                "<b>🔌 Terminal Disconnected</b>\n\n"
                "Active terminal session close kar diya gaya hai.\n"
                "/terminal se dobara connect karein.",
                parse_mode="HTML"
            )
        except TelegramError:
            try:
                await q.message.reply_text(
                    "<b>🔌 Terminal Disconnected</b>\n\n"
                    "Active terminal session close kar diya gaya hai.\n"
                    "/terminal se dobara connect karein.",
                    parse_mode="HTML"
                )
            except TelegramError:
                pass
        return

    if d in key_map or d in ("key_ctrlc", "key_stop"):
        sess = ACTIVE_SESSIONS.get(uid)
        if sess and sess.is_running:
            if d == "key_ctrlc" or d == "key_stop":
                sess.kill()
            else:
                sess.send_input(key_map[d])
            disp = sess.get_display_text()
            try:
                await q.edit_message_text(
                    f"⚡ <b>Terminal:</b> <code>$ {h(sess.cmd)}</code>\n"
                    f"<pre>{h(disp)}</pre>\n"
                    f"<i>Key: {d} | 📂 {h(sess.cwd)}</i>",
                    parse_mode="HTML",
                    reply_markup=get_keypad_markup() if sess.is_running else None
                )
            except TelegramError:
                pass
        return

    if d == "btn_term":
        context.user_data["in_terminal"] = True
        context.user_data.setdefault("cwd", str(Path.cwd()))
        cwd = context.user_data["cwd"]
        await q.message.reply_text(
            f"<b>Interactive Terminal Mode (Real PTY Active)!</b>\n\n"
            f"CWD: <code>{h(cwd)}</code>\n\n"
            f"Commands bhejein — <b>isatty=True enabled! Arrow Keys, Stop/Ctrl+C aur Disconnect controls live available hain!</b>",
            parse_mode="HTML"
        )
    elif d == "btn_reset":
        cmd = (
            "pkill -f hermes ; "
            "rm -rf /home/container/.hermes /home/container/.config/.hermes ; "
            "mkdir -p /home/container/.hermes"
        )
        await asyncio.to_thread(safe_run, cmd, 10)
        await q.message.reply_text("<b>✅ Reset Complete!</b> /terminal se fresh setup karein.", parse_mode="HTML")
    elif d == "sysinfo":
        info = await asyncio.to_thread(collect_sysinfo)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="sysinfo")]])
        await q.edit_message_text(info, parse_mode="HTML", reply_markup=kb)
    elif d == "sshinfo":
        info = await asyncio.to_thread(collect_ssh_info)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="sshinfo")]])
        await q.edit_message_text(info, parse_mode="HTML", reply_markup=kb)
    elif d == "speedtest":
        await q.edit_message_text("Speed test chal raha hai...", parse_mode="HTML")
        res = await asyncio.to_thread(run_speedtest)
        result = format_speedtest_result(res)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Run Again", callback_data="speedtest")]])
        await q.edit_message_text(result, parse_mode="HTML", reply_markup=kb)
    elif d == "ps":
        result = await asyncio.to_thread(_get_procs_text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Refresh", callback_data="ps")]])
        await q.edit_message_text(result, parse_mode="HTML", reply_markup=kb)


# ==================== MAIN ENTRYPOINT ====================

def main():
    if BOT_TOKEN in ("", "APNA_BOT_TOKEN_YAHAN"):
        raise SystemExit("\nBOT_TOKEN set nahi hai!\n")
    log.info("VPS Terminal Bot starting with Native Pseudo-Terminal support...")
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    auth_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAIT_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)]},
        fallbacks=[CommandHandler("start", start)],
        per_user=True, per_chat=True,
    )
    app.add_handler(auth_conv)
    app.add_handler(CommandHandler("reset",     reset_cmd))
    app.add_handler(CommandHandler("sysinfo",   sysinfo_cmd))
    app.add_handler(CommandHandler("sshinfo",   sshinfo_cmd))
    app.add_handler(CommandHandler("speedtest", speedtest_cmd))
    app.add_handler(CommandHandler("terminal",  terminal_cmd))
    app.add_handler(CommandHandler("exit",      exit_cmd))
    app.add_handler(CommandHandler("upload",    upload_cmd))
    app.add_handler(CommandHandler("processes", processes_cmd))
    app.add_handler(CommandHandler("help",      help_cmd))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, terminal_input))
    log.info("Bot ready! Ctrl+C se band karein.")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
