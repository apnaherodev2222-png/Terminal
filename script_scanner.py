
import ast
import re
import sqlite3
from pathlib import Path

# Shared DB with hosting_panel.py/approval_bot.py. Custom patterns added by
# an admin through the bot (Admin Panel -> Scanner Patterns) land in
# custom_scan_patterns and are picked up here on every scan — no restart,
# no editing this file.
_DB_PATH = Path(__file__).parent / 'inf' / 'bot_data.db'

# (pattern, short category label, severity)
SUSPICIOUS_PATTERNS = [
    # Network Abuse & DoS
    (r'\bhping3\b', 'packet-flood tooling', 'high'),
    (r'\bmasscan\b', 'mass network scanning tooling', 'high'),
    (r'\bnmap\b[^\n]{0,40}-p\s*0-65535', 'mass port scanning', 'high'),
    (r'\bslowloris\b', 'DoS pattern', 'high'),
    (r'\bLOIC\b|\bHOIC\b', 'known DDoS tool reference', 'high'),
    (r'SYN[_ -]?flood|UDP[_ -]?flood|ICMP[_ -]?flood', 'flood-attack pattern', 'high'),
    (r'\bscapy\b[\s\S]{0,60}(send|sr1|flood)', 'raw packet crafting/flood', 'high'),
    (r'\bsocket\.SOCK_RAW\b', 'raw socket usage', 'medium'),

    # Brute-force & Exploits
    (r'\bhydra\b|\bmedusa\b|\bncrack\b', 'credential brute-force tooling', 'high'),
    (r'\bsqlmap\b', 'SQL-injection exploitation tooling', 'high'),
    (r'while\s+True\s*:[\s\S]{0,80}requests\.(get|post)', 'unbounded HTTP flood loop', 'medium'),

    # Reverse Shells & Remote Access
    (r'subprocess\.Popen\([^\n]*\b(nc|netcat|bash|sh|cmd\.exe|powershell)\b', 'reverse shell execution pattern', 'high'),
    (r'socket\.[Ss]ocket\([\s\S]{0,100}\.connect\([^\n]*(subprocess|os\.dup2|pty\.spawn)', 'interactive reverse shell', 'high'),
    (r'\b(exec|eval)\s*\(\s*(base64|bz2|zlib|codecs)\b', 'obfuscated/encoded payload execution', 'high'),
    (r'\bimport\s+pty\b[\s\S]{0,50}pty\.spawn', 'pty spawn for remote shell', 'high'),
    (r'(/bin/sh|/bin/bash|cmd\.exe)\b[^\n]{0,30}-i\b', 'interactive shell redirection', 'high'),

    # Cryptomining & Botnets
    (r'\bxmrig\b|stratum\+tcp://|\bcryptonight\b', 'crypto-mining indicators', 'high'),
    (r'/etc/shadow|/etc/passwd', 'sensitive host file access', 'medium'),
    (r'\bparamiko\b', 'SSH client library usage', 'medium'),
    (r'\btelnetlib\b|\btelnetlib3\b', 'raw telnet client usage', 'medium'),
    (r'\bbotnet\b|\bC2[_ -]?server\b|command[_ -]?and[_ -]?control', 'botnet/C2 terminology', 'medium'),
    (r'\bmirai\b|\bgafgyt\b|\bqbot\b', 'known IoT-botnet family reference', 'high'),

    # Destructive System Commands
    (r'rm\s+-rf\s+/(?!\S)', 'destructive filesystem wipe', 'high'),
    (r'os\.system\([^\n]*mkfs', 'disk formatting command', 'high'),
    (r'\bshodan\b|\bcensys\b|\bzoomeye\b', 'internet-wide host search API usage', 'medium'),

    # Credential Harvesting & Exfiltration
    (r'(wordlist|combolist|userlist|passlist|creds?_list)\s*=', 'bulk credential-list usage', 'medium'),
    (r"(admin|root)['\"]?\s*[,:]\s*['\"](admin|root|password|toor|12345)", 'default-credential list', 'medium'),
    (r'ip_network\([\s\S]{0,200}(socket\.connect|\.connect_ex|paramiko|telnetlib)', 'IP-range iteration + connection attempt', 'high'),
    (r'for\s+\w+\s+in\s+range\([\s\S]{0,120}\)\s*:[\s\S]{0,200}socket\.connect', 'ranged loop + raw socket connect', 'medium'),
    (r'ThreadPoolExecutor[\s\S]{0,150}(socket\.connect|paramiko|telnetlib)', 'multi-threaded mass connection attempts', 'high'),
    (r'(Path\(\s*[\'"]/[\'"]\s*\)|os\.walk\(\s*[\'"]/[\'"]\s*\))', 'recursive scan starting at filesystem root', 'high'),
    (r'id_rsa[\s\S]{0,200}authorized_keys|authorized_keys[\s\S]{0,200}id_rsa', 'SSH key/credential harvesting', 'high'),
    (r'-----BEGIN[^\n]{0,20}PRIVATE KEY-----', 'private-key content or private-key search pattern', 'high'),
    (r'\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{20,}\b|\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bsk_live_[A-Za-z0-9]{20,}\b', 'cloud/service credential value detected', 'high'),
    (r'\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b', 'hardcoded Telegram bot token (normal for simple bots — only a real problem combined with other flags)', 'medium'),
    (r'(sendDocument|discord\.com/api/webhooks)[\s\S]{0,300}(zipfile|zipf\.write|rglob|os\.walk)', 'bulk file exfiltration to external chat/webhook', 'high'),
    (r'\bexfil(trat|_dir|_targets)\b', 'exfiltration-labeled code', 'high'),

    # Anti-Analysis & Evasion Patterns
    (r'ctypes\.windll|ptrace\(', 'anti-debugging/sandbox evasion technique', 'high'),
    (r'\bsys\.settrace\b', 'runtime trace-hooking (also used by legit profilers/coverage tools)', 'medium'),
    (r'urllib\.request\.urlopen\([^\n]*raw\.githubusercontent\.com[^\n]*\.(exe|sh|py|elf)', 'remote dropper script pattern', 'high'),

    # Obfuscation (ported from security_scanner_free.py — regex-visible cases;
    # the AST layer below catches the aliased-import case these miss)
    (r'(?:\\x[0-9a-fA-F]{2}){6,}', 'long hex-escape string — likely obfuscated payload', 'medium'),
    (r'chr\s*\(\s*\d+\s*\)\s*\+\s*chr\s*\(\s*\d+\s*\)', 'chr() concatenation chain — character-code obfuscation', 'medium'),

    # Suspicious network destinations (ported from security_scanner_free.py)
    (r'pastebin\.com/raw', 'remote code fetch from Pastebin raw', 'high'),
    (r'\bngrok\b|\blocaltunnel\b|\bserveo\.net\b', 'tunneling service — exposes a local port publicly', 'medium'),
]

MAX_SCAN_BYTES = 2_000_000  # 2MB tak ki limit taaki bade files system ko choke na karein


def load_custom_patterns(db_path: Path = None):
    """
    Pulls admin-added patterns from custom_scan_patterns (active=1 only).
    Bad regex from a typo is skipped rather than crashing every scan —
    validate_pattern() below is what the bot uses to catch typos before
    they're ever saved.
    Returns a list in the same (pattern, label, severity) shape as
    SUSPICIOUS_PATTERNS. Empty list (not an error) if the DB/table isn't
    there yet, e.g. on a machine only running this scanner standalone.
    """
    path = db_path or _DB_PATH
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(str(path))
        rows = conn.execute(
            "SELECT pattern, label, severity FROM custom_scan_patterns WHERE active = 1"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []

    valid = []
    for pattern, label, severity in rows:
        try:
            re.compile(pattern)
        except re.error:
            continue  # skip a bad pattern instead of taking the whole scanner down
        valid.append((pattern, label, severity if severity in ('high', 'medium') else 'medium'))
    return valid


def validate_pattern(pattern: str):
    """Used by the bot before saving a new pattern, so a typo gets caught
    immediately instead of silently doing nothing on every future scan."""
    try:
        re.compile(pattern)
        return True, None
    except re.error as e:
        return False, str(e)


# ── AST deep-scan (Python files only) ─────────────────────────────────
# Regex only sees text; this sees actual code STRUCTURE, so it catches
# "eval/exec called with a computed/dynamic argument" no matter what the
# attacker names their variables or aliases their imports — the exact
# class of evasion that let a real backdoor slip past a regex-only scanner
# (`import zlib as _zl` defeated a `zlib\.decompress` literal-name check).
_SENSITIVE_DIRS = {'/', '/root', '/etc', '/home', '/proc', '/var', '/sys'}
_SUSPICIOUS_CLASS_WORDS = {'harvest', 'steal', 'exfil', 'harvester', 'collector', 'grabber'}


def _ast_findings(text: str):
    """Returns findings in the same (label, severity) shape the rest of
    this module uses. Only meaningful for Python source — callers should
    only invoke this for .py files (see scan_file below)."""
    findings = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Not itself proof of anything malicious (could just be a partial
        # file, or genuinely-broken code) — low severity, only matters
        # combined with something else.
        findings.append(('code failed to parse as valid Python — possibly obfuscated/corrupted', 'medium'))
        return findings
    except (ValueError, RecursionError):
        # Extremely deep/malformed trees can blow the recursion limit —
        # fail closed rather than crashing the whole scan.
        findings.append(('code structure too complex/malformed to analyze safely', 'medium'))
        return findings

    seen = set()

    def add(label, severity):
        if label not in seen:
            seen.add(label)
            findings.append((label, severity))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func

            if isinstance(func, ast.Name):
                fid = func.id
                # eval/exec with a DYNAMIC argument (a call or attribute
                # access, e.g. exec(compile(_zl.decompress(x))) or
                # eval(some_module.decode())) rather than a plain string
                # literal. A literal eval("1+1") is noise; a computed one
                # is the actual red flag.
                if fid in ('eval', 'exec') and node.args:
                    arg0 = node.args[0]
                    if isinstance(arg0, (ast.Call, ast.Attribute, ast.BinOp)):
                        add(f'{fid}() called with a dynamically-computed argument — hidden/decoded code execution', 'high')

                # __import__('os') / __import__('subprocess') as a real call
                # (not just the string appearing somewhere) — common way to
                # dodge a static "import os" text-grep.
                if fid == '__import__' and node.args:
                    arg0 = node.args[0]
                    if isinstance(arg0, ast.Constant) and arg0.value in ('os', 'subprocess', 'ctypes'):
                        add(f"dynamic __import__('{arg0.value}') — avoids a plain import statement", 'medium')

            if isinstance(func, ast.Attribute):
                # os.walk('/'), os.walk('/root'), etc — a literal sensitive
                # directory, not a user-controlled/relative path.
                if (func.attr == 'walk' and isinstance(func.value, ast.Name)
                        and func.value.id == 'os' and node.args):
                    arg0 = node.args[0]
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str) and arg0.value in _SENSITIVE_DIRS:
                        add(f"os.walk('{arg0.value}') — scanning a system directory, not its own folder", 'high')

        # Class names like "CredentialHarvester", "DataStealer" — a script
        # calling itself this is telling on itself.
        if isinstance(node, ast.ClassDef):
            name_lower = node.name.lower()
            if any(w in name_lower for w in _SUSPICIOUS_CLASS_WORDS):
                add(f"class name '{node.name}' suggests data harvesting/exfiltration", 'medium')

    return findings


def scan_file(path: Path):
    """
    Returns (verdict, findings):
      verdict   'clear' | 'flagged'
      findings  list of (category_label, severity) tuples that matched

    Verdict logic: agar koi bhi 'high' severity hit milta hai, ya 2 ya usse
    zyada total hits milte hain, toh script automatically reject/flag ho
    jaayegi. Two layers feed into the same findings list: regex patterns
    (broad, fast) and — for .py files only — an AST structural scan that
    catches dynamic eval/exec/import patterns regex alone can miss.
    """
    path = Path(path)
    try:
        file_size = path.stat().st_size
    except Exception:
        # Can't even stat the file — fail CLOSED (flag for review), not
        # open. A read/stat failure is not evidence of safety.
        return 'flagged', [('scanner could not read file (auto-flagged, fail-closed)', 'high')]

    findings = []
    all_patterns = SUSPICIOUS_PATTERNS + load_custom_patterns()

    # Scan the WHOLE file rather than silently truncating at MAX_SCAN_BYTES
    # and calling the untouched remainder "clear" — that truncation was a
    # trivial bypass (pad the file past the cutoff, put the payload after
    # it). Oversized files are scanned in chunks with a small overlap so a
    # pattern straddling a chunk boundary still matches; if a file is so
    # large that even chunked scanning is impractical, it's flagged for
    # manual review instead of being waved through unscanned (and we never
    # even load it fully into memory in that case).
    if file_size > MAX_SCAN_BYTES * 20:
        findings.append((f'file too large to scan safely ({file_size} bytes) — sent for manual review', 'high'))
        return 'flagged', findings

    try:
        text = path.read_bytes().decode('utf-8', errors='ignore')
    except Exception:
        return 'flagged', [('scanner could not read file (auto-flagged, fail-closed)', 'high')]

    overlap = 256
    pos = 0
    text_len = len(text)
    seen_labels = set()
    while pos < text_len:
        chunk = text[pos:pos + MAX_SCAN_BYTES]
        for pattern, label, severity in all_patterns:
            if label in seen_labels:
                continue
            if re.search(pattern, chunk, re.IGNORECASE):
                findings.append((label, severity))
                seen_labels.add(label)
        pos += MAX_SCAN_BYTES - overlap

    # AST layer — Python files only; ast.parse() doesn't understand
    # JavaScript, so running this on a .js file would just produce a
    # meaningless "failed to parse" hit on every single upload.
    if path.suffix.lower() == '.py':
        for label, severity in _ast_findings(text):
            if label not in seen_labels:
                findings.append((label, severity))
                seen_labels.add(label)

    has_high = any(sev == 'high' for _, sev in findings)
    verdict = 'flagged' if has_high or len(findings) >= 2 else 'clear'
    return verdict, findings
