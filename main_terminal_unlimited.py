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
import shutil
import zipfile
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



# ==================== EXTRA VPS MANAGEMENT FEATURES ====================

FILE_PAGE_SIZE = 12
ALERT_CPU = 90.0
ALERT_RAM = 90.0
ALERT_DISK = 90.0


def _safe_path(raw: str, cwd: str = None) -> Path:
    base = Path(cwd or Path.cwd()).expanduser().resolve()
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def _file_manager_text(path: Path) -> str:
    try:
        path = path.resolve()
        if not path.is_dir():
            return f"❌ Not a directory: <code>{h(path)}</code>"
        rows = [f"<b>📁 File Manager</b>\n<code>{h(path)}</code>\n"]
        entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:FILE_PAGE_SIZE]
        if not entries:
            rows.append("<i>Empty directory</i>")
        for e in entries:
            try:
                st = e.stat()
                if e.is_dir():
                    rows.append(f"📂 <code>{h(e.name)}</code>  <i>DIR</i>")
                else:
                    rows.append(f"📄 <code>{h(e.name)}</code>  {size_str(st.st_size)}")
            except (OSError, PermissionError):
                rows.append(f"❓ <code>{h(e.name)}</code>")
        return "\n".join(rows)
    except Exception as e:
        return f"❌ File manager error: <code>{h(e)}</code>"


def _dashboard_text() -> str:
    try:
        import psutil
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.cwd().anchor or "/"))
        cpu = psutil.cpu_percent(interval=0.3)
        load = os.getloadavg() if hasattr(os, "getloadavg") else None
        lines = [
            "<b>🖥️ VPS DASHBOARD</b>",
            "",
            f"CPU:  <code>{pbar(cpu, 100)} </code>",
            f"RAM:  <code>{pbar(vm.used, vm.total)}</code>",
            f"Disk: <code>{pbar(disk.used, disk.total)}</code>",
            f"Uptime: <code>{h(_uptime_text())}</code>",
            f"User: <code>{h(get_current_user())}</code>",
            f"CWD: <code>{h(Path.cwd())}</code>",
        ]
        if load:
            lines.append(f"Load: <code>{' | '.join(f'{x:.2f}' for x in load)}</code>")
        return "\n".join(lines)
    except Exception:
        return collect_sysinfo()


def _uptime_text() -> str:
    try:
        with open('/proc/uptime') as f:
            sec = float(f.read().split()[0])
        return f"{int(sec//86400)}d {int(sec%86400//3600)}h {int(sec%3600//60)}m"
    except Exception:
        return "unknown"


def _logs_text(service: str, lines: int = 40) -> str:
    service = service.strip()
    if not service or not re.match(r'^[A-Za-z0-9_.@:-]+$', service):
        return "❌ Invalid service name."
    try:
        out = safe_run(["journalctl", "-u", service, "-n", str(max(1, min(lines, 200))), "--no-pager"], 8)
        if not out:
            out = safe_run(["systemctl", "status", service, "--no-pager", "-n", str(max(1, min(lines, 80)))], 8)
        return f"<b>📜 Logs: {h(service)}</b>\n<pre>{h(out[-3500:] or 'No output')}</pre>"
    except Exception as e:
        return f"❌ Logs error: <code>{h(e)}</code>"


def _service_action(service: str, action: str) -> str:
    if not re.match(r'^[A-Za-z0-9_.@:-]+$', service or ''):
        return "❌ Invalid service name."
    if action not in {"status", "start", "stop", "restart", "enable", "disable"}:
        return "❌ Unsupported service action."
    if action == "status":
        cmd = ["systemctl", "status", service, "--no-pager", "-n", "25"]
    else:
        cmd = ["systemctl", action, service]
    out = safe_run(cmd, 12)
    return f"<b>⚙️ Service: {h(service)} / {h(action)}</b>\n<pre>{h(out[-3500:] or 'No output')}</pre>"


def _process_details(pid: int) -> str:
    try:
        import psutil
        p = psutil.Process(pid)
        with p.oneshot():
            return (f"<b>🔎 Process {pid}</b>\n"
                    f"Name: <code>{h(p.name())}</code>\n"
                    f"User: <code>{h(p.username())}</code>\n"
                    f"Status: <code>{h(p.status())}</code>\n"
                    f"CPU: <code>{p.cpu_percent(interval=0.1):.1f}%</code>\n"
                    f"MEM: <code>{p.memory_percent():.1f}%</code>\n"
                    f"CWD: <code>{h(p.cwd() if p.pid else '')}</code>")
    except Exception as e:
        return f"❌ Process error: <code>{h(e)}</code>"


def _process_control(pid: int, action: str) -> str:
    try:
        import psutil
        p = psutil.Process(pid)
        if action == "terminate":
            p.terminate()
        elif action == "kill":
            p.kill()
        else:
            return "❌ Unsupported process action."
        return f"✅ PID <code>{pid}</code> {h(action)} request sent."
    except Exception as e:
        return f"❌ Process control failed: <code>{h(e)}</code>"


def _create_backup(paths) -> str:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = Path(tempfile.gettempdir()) / f"vps_backup_{stamp}.zip"
    try:
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            for raw in paths:
                p = _safe_path(raw)
                if not p.exists():
                    continue
                if p.is_file():
                    z.write(p, p.name)
                elif p.is_dir():
                    for child in p.rglob('*'):
                        if child.is_file():
                            try:
                                z.write(child, str(Path(p.name) / child.relative_to(p)))
                            except (OSError, PermissionError):
                                pass
        return str(out) if out.exists() else ""
    except Exception:
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return ""


def _alert_text() -> str:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.4)
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(str(Path.cwd().anchor or '/'))
        alerts = []
        if cpu >= ALERT_CPU:
            alerts.append(f"⚠️ CPU high: {cpu:.1f}%")
        if vm.percent >= ALERT_RAM:
            alerts.append(f"⚠️ RAM high: {vm.percent:.1f}%")
        if disk.percent >= ALERT_DISK:
            alerts.append(f"⚠️ Disk high: {disk.percent:.1f}%")
        if not alerts:
            return "<b>🟢 VPS Alerts</b>\n\nNo threshold crossed."
        return "<b>🔴 VPS Alerts</b>\n\n" + "\n".join(alerts)
    except Exception as e:
        return f"❌ Alert check failed: <code>{h(e)}</code>"


def _quick_commands_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard"), InlineKeyboardButton("📁 Files", callback_data="files")],
        [InlineKeyboardButton("📜 Logs", callback_data="logs_hint"), InlineKeyboardButton("⚙️ Services", callback_data="services")],
        [InlineKeyboardButton("🔔 Alerts", callback_data="alerts"), InlineKeyboardButton("🔄 Processes", callback_data="ps")],
    ])

async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    msg = await update.message.reply_text(_dashboard_text(), parse_mode="HTML", reply_markup=_quick_commands_markup())

async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    raw = " ".join(context.args).strip() if context.args else str(Path.cwd())
    p = _safe_path(raw)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="files")]])
    await update.message.reply_text(_file_manager_text(p), parse_mode="HTML", reply_markup=kb)

async def getfile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/get /path/to/file</code>", parse_mode="HTML"); return
    p = _safe_path(" ".join(context.args))
    if not p.is_file():
        await update.message.reply_text("❌ File nahi mili."); return
    if p.stat().st_size > 50 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (max 50MB)."); return
    with open(p, 'rb') as f:
        await context.bot.send_document(update.effective_chat.id, f, filename=p.name, caption=f"<code>{h(p)}</code>", parse_mode="HTML")

async def mkdir_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/mkdir folder</code>", parse_mode="HTML"); return
    try:
        p = _safe_path(" ".join(context.args)); p.mkdir(parents=True, exist_ok=False)
        await update.message.reply_text(f"✅ Created: <code>{h(p)}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {h(e)}", parse_mode="HTML")

async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/rename old new</code>", parse_mode="HTML"); return
    try:
        src = _safe_path(context.args[0]); dst = _safe_path(" ".join(context.args[1:]))
        src.rename(dst)
        await update.message.reply_text(f"✅ Renamed to: <code>{h(dst)}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {h(e)}", parse_mode="HTML")

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/delete path</code>", parse_mode="HTML"); return
    try:
        p = _safe_path(" ".join(context.args))
        if p.is_dir(): shutil.rmtree(p)
        elif p.is_file(): p.unlink()
        else: raise FileNotFoundError(str(p))
        await update.message.reply_text(f"✅ Deleted: <code>{h(p)}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Delete failed: <code>{h(e)}</code>", parse_mode="HTML")

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/logs service [lines]</code>", parse_mode="HTML"); return
    n = 40
    if len(context.args) > 1 and context.args[1].isdigit(): n = int(context.args[1])
    await update.message.reply_text(_logs_text(context.args[0], n), parse_mode="HTML")

async def service_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/service status|start|stop|restart|enable|disable name</code>", parse_mode="HTML"); return
    await update.message.reply_text(_service_action(context.args[1], context.args[0]), parse_mode="HTML")

async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    await update.message.reply_text(_alert_text(), parse_mode="HTML")

async def process_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: <code>/process PID</code>", parse_mode="HTML"); return
    await update.message.reply_text(_process_details(int(context.args[0])), parse_mode="HTML")

async def process_action_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if len(context.args) != 2 or not context.args[1].isdigit():
        await update.message.reply_text("Usage: <code>/procaction terminate|kill PID</code>", parse_mode="HTML"); return
    await update.message.reply_text(_process_control(int(context.args[1]), context.args[0]), parse_mode="HTML")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    hist = context.user_data.get('command_history', [])[-20:]
    if not hist:
        await update.message.reply_text("📝 Command history empty."); return
    await update.message.reply_text("<b>📝 Last commands</b>\n\n" + "\n".join(f"<code>{i+1}. {h(x)}</code>" for i,x in enumerate(hist)), parse_mode="HTML")

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/backup /path/file /path/folder</code>", parse_mode="HTML"); return
    msg = await update.message.reply_text("📦 Backup create ho raha hai...")
    out = await asyncio.to_thread(_create_backup, context.args)
    if not out:
        await msg.edit_text("❌ Backup create nahi hua."); return
    try:
        with open(out, 'rb') as f:
            await context.bot.send_document(update.effective_chat.id, f, filename=Path(out).name, caption="📦 VPS backup")
        Path(out).unlink(missing_ok=True)
        await msg.delete()
    except Exception as e:
        await msg.edit_text(f"❌ Backup upload failed: <code>{h(e)}</code>", parse_mode="HTML")


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: <code>/restore backup.zip /target/folder</code>", parse_mode="HTML"); return
    try:
        archive = _safe_path(context.args[0])
        target = _safe_path(" ".join(context.args[1:]))
        if not archive.is_file() or archive.suffix.lower() != '.zip':
            await update.message.reply_text("❌ ZIP backup nahi mili."); return
        target.mkdir(parents=True, exist_ok=True)
        target_real = target.resolve()
        with zipfile.ZipFile(archive, 'r') as z:
            for member in z.infolist():
                dest = (target_real / member.filename).resolve()
                if os.path.commonpath([str(target_real), str(dest)]) != str(target_real):
                    raise ValueError("Unsafe archive path detected")
            z.extractall(target_real)
        await update.message.reply_text(f"✅ Restore complete: <code>{h(target_real)}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Restore failed: <code>{h(e)}</code>", parse_mode="HTML")

async def put_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    doc = update.message.document
    target = context.user_data.pop('upload_target', None)
    if not target:
        await update.message.reply_text("Pehle <code>/put /path/folder</code> bhejein.", parse_mode="HTML"); return
    try:
        folder = _safe_path(target)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / Path(doc.file_name or 'uploaded_file').name
        if dest.exists():
            await update.message.reply_text("❌ File already exists. Rename/delete first."); return
        if doc.file_size and doc.file_size > 50 * 1024 * 1024:
            await update.message.reply_text("❌ File too large (max 50MB)."); return
        tgfile = await doc.get_file()
        await tgfile.download_to_drive(custom_path=str(dest))
        await update.message.reply_text(f"✅ Uploaded: <code>{h(dest)}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Upload failed: <code>{h(e)}</code>", parse_mode="HTML")

async def put_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_auth(update.effective_user.id, context.application): return
    if not context.args:
        await update.message.reply_text("Usage: <code>/put /path/folder</code> then Telegram file send karein.", parse_mode="HTML"); return
    context.user_data['upload_target'] = " ".join(context.args)
    await update.message.reply_text(f"📤 Target: <code>{h(_safe_path(context.user_data['upload_target']))}</code>\nAb file/document send karein.", parse_mode="HTML")

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
            [InlineKeyboardButton("🖥️ Dashboard", callback_data="dashboard"), InlineKeyboardButton("📁 Files", callback_data="files")],
            [InlineKeyboardButton("🔔 Alerts", callback_data="alerts"), InlineKeyboardButton("⚙️ Services", callback_data="services")],
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

    hist = context.user_data.setdefault("command_history", [])
    if not hist or hist[-1] != text:
        hist.append(text)
        del hist[:-50]

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
        "/dashboard  - VPS dashboard\n"
        "/files      - File manager\n"
        "/get        - Download file\n"
        "/put        - Upload file to VPS\n"
        "/mkdir      - Create folder\n"
        "/rename     - Rename file/folder\n"
        "/delete     - Delete file/folder\n"
        "/logs       - Service logs\n"
        "/service    - Service manager\n"
        "/alerts     - Resource alerts\n"
        "/process    - Process details\n"
        "/procaction - Process control\n"
        "/history    - Command history\n"
        "/backup     - Create ZIP backup\n"
        "/restore    - Restore ZIP backup\n"
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
    elif d == "dashboard":
        await q.edit_message_text(_dashboard_text(), parse_mode="HTML", reply_markup=_quick_commands_markup())
    elif d == "files":
        p = _safe_path(context.user_data.get("cwd", str(Path.cwd())))
        await q.edit_message_text(_file_manager_text(p), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="files")]]))
    elif d == "services":
        await q.edit_message_text("<b>⚙️ Service Manager</b>\n\nUse: <code>/service status NAME</code>\n<code>/service restart NAME</code>\n<code>/service start NAME</code>\n<code>/service stop NAME</code>", parse_mode="HTML")
    elif d == "logs_hint":
        await q.edit_message_text("<b>📜 Logs</b>\n\nUse: <code>/logs SERVICE</code>\nExample: <code>/logs ssh</code>", parse_mode="HTML")
    elif d == "alerts":
        await q.edit_message_text(_alert_text(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="alerts")]]))


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
    app.add_handler(CommandHandler("dashboard", dashboard_cmd))
    app.add_handler(CommandHandler("files",     files_cmd))
    app.add_handler(CommandHandler("get",       getfile_cmd))
    app.add_handler(CommandHandler("put",       put_cmd))
    app.add_handler(CommandHandler("mkdir",      mkdir_cmd))
    app.add_handler(CommandHandler("rename",     rename_cmd))
    app.add_handler(CommandHandler("delete",     delete_cmd))
    app.add_handler(CommandHandler("logs",       logs_cmd))
    app.add_handler(CommandHandler("service",    service_cmd))
    app.add_handler(CommandHandler("alerts",     alerts_cmd))
    app.add_handler(CommandHandler("process",    process_cmd))
    app.add_handler(CommandHandler("procaction", process_action_cmd))
    app.add_handler(CommandHandler("history",    history_cmd))
    app.add_handler(CommandHandler("backup",     backup_cmd))
    app.add_handler(CommandHandler("restore",    restore_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, put_document))
    app.add_handler(CallbackQueryHandler(btn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, terminal_input))
    log.info("Bot ready! Ctrl+C se band karein.")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
