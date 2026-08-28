"""TeamSync - a window over the two-way sync engine.

Role   : let two people share one folder through a private GitHub repository
         without typing git commands.
Input  : a folder (new shared project), or a repository name (joining one).
Output : a running sync daemon, a live log, and a Publish button.
Never  : force-pushes, deletes work, or hides a conflict.

The engine is the PowerShell scripts in engine/. This file only drives them and
shows what they are doing, so there is exactly one implementation of the git
logic and it is the one that was tested.

The engine runs DETACHED: closing this window does not stop it. The window shows
what the engine does by tailing .teamsync.log, and finds a running engine again
through the .teamsync.lock heartbeat. Stopping is always explicit (Stop sync /
Disconnect) - never a side effect of closing a window.
"""

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import sv_ttk
except Exception:                     # packaging or antivirus can lose it
    sv_ttk = None

APP_NAME = "TeamSync"
APP_VERSION = "2.0.22"          # compared against the newest release tag
UPDATE_REPO = "aminbm1919/teamsync-app"   # private; both sides have access
# The app ships as a folder, not a single file. A one-file build unpacks ~970
# files into %TEMP% on every launch, and on a machine whose antivirus interferes
# with that, a file is sometimes missing - which showed up as three different
# fatal errors before the cause was clear. A folder is never unpacked at all.
PACKAGE_NAME = "TeamSync.zip"
UPDATE_EVERY_SECONDS = 15      # cheap: a 304 costs nothing, see latest_release()
# Auto-update means the machine that publishes a release can run code on the
# other machine. That is too much power to leave resting on one GitHub account,
# so a build is only ever run if it carries a signature from this exact key.
# The matching private key is passphrase-protected and never leaves its owner.
RELEASE_PUBKEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOlpU5x3Y/XUpN6lwFpU4G/CtXw8YcD2u4hPjosVDCEr teamsync release signing'
SIGN_NAMESPACE = "teamsync"
POWERSHELL = "powershell.exe"
CREATE_NO_WINDOW = 0x08000000
N = chr(10)
NN = N + N
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# ---------------------------------------------------------------- plumbing ---


def resource_path(*parts):
    """Locate a bundled file, whether running from source or from the exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidate = os.path.join(base, *parts)
    if os.path.exists(candidate):
        return candidate
    # running from source: engine scripts sit one level up
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", parts[-1])


def config_path():
    # Roaming AppData, not Local: Store-installed Python silently redirects some
    # Local writes, which would make the exe and a source run disagree.
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(root, APP_NAME)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


def git(repo, *args):
    """Run one git command in repo. Returns stdout stripped, or '' on failure."""
    try:
        out = subprocess.run(
            ["git"] + list(args), cwd=repo, capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def pid_alive(pid):
    """Is this Windows process still running?"""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    try:
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:
        return False


def daemon_pid(repo):
    """PID of a live engine for this repo, or None.

    The engine rewrites .teamsync.lock every second, so a fresh mtime plus a live
    PID means an engine is covering this folder - possibly one started by a
    window that has since been closed.
    """
    lock = os.path.join(repo, ".teamsync.lock")
    try:
        import time
        if time.time() - os.path.getmtime(lock) > 30:
            return None
        # utf-8-sig, not utf-8: Windows PowerShell 5.1 writes a byte-order mark
        # at the head of the file, which makes the first line "﻿pid=..." and
        # not "pid=...". Read as plain utf-8 this returned None for a perfectly
        # healthy engine, so a window could never reconnect to one left running
        # in the background - the whole point of the engine being detached.
        with open(lock, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                if line.startswith("pid="):
                    pid = int(line.strip().split("=", 1)[1])
                    return pid if pid_alive(pid) else None
    except Exception:
        pass
    return None


def daemon_state(repo):
    """Read what the engine last wrote about itself: net status and backlog."""
    state = {}
    try:
        with open(os.path.join(repo, ".teamsync.lock"), "r",
                  encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    state[k] = v
    except OSError:
        pass
    return state


def kill_pid(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def partner_presence(repo, my_name):
    """Who else has beaten recently, and how long ago.

    The engine publishes a heartbeat as a ref named
    refs/teamsync/presence/<name>/<unix-seconds> and fetches the other side's.
    A ref carries no date, so the timestamp lives in the name. Returns
    (name, seconds_ago) for the freshest partner beat, or (None, None).
    """
    import time
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/presence")
    best_name, best_ts = None, -1
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) < 5:
            continue
        name, ts = parts[3], parts[4]
        if name == my_name:
            continue
        try:
            ts = int(ts)
        except ValueError:
            continue
        if ts > best_ts:
            best_name, best_ts = name, ts
    if best_name is None:
        return None, None
    return best_name, max(0, int(time.time()) - best_ts)


def partner_pending_files(repo, my_name):
    """Files the partner has hands on right now, decoded from the pending refs.

    The engine keeps refs/teamsync/pending/<name>/<hex-of-path> fetched; this
    just reads and decodes them. It is the data behind the live "working on"
    line - the user asked for a standing display, not a log line that scrolls
    past in a foreign language.
    """
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/pending")
    files = []
    for line in out.splitlines():
        parts = line.strip().split("/", 4)
        if len(parts) < 5 or parts[3] == my_name:
            continue
        try:
            files.append(bytes.fromhex(parts[4]).decode("utf-8", "replace"))
        except ValueError:
            continue
    return sorted(set(files))


def clear_my_presence(repo, my_name):
    """Remove my own heartbeat from the server.

    The engine tries to do this on a graceful exit, but Stop and Disconnect kill
    it outright, and a killed process runs no cleanup. Without this the partner
    would keep seeing me as online for a couple of minutes after I stopped.
    A machine losing power is still unavoidable - that beat just goes stale.
    """
    if not my_name:
        return
    out = git(repo, "ls-remote", "origin", f"refs/teamsync/presence/{my_name}/*")
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip():
            subprocess.run(["git", "push", "-q", "origin", ":" + parts[1].strip()],
                           cwd=repo, capture_output=True, creationflags=CREATE_NO_WINDOW)


def presence_name(repo):
    """The same sanitised name the engine publishes under."""
    import re
    n = git(repo, "config", "user.name") or os.environ.get("USERNAME", "")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", n).strip("-")


def humanise(seconds):
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"




# --------------------------------------------------------- one copy only ----
# Two windows on the same machine double every log line, fight over the same
# project, and - the reason this was found - hold the program file open so that
# neither can replace it when an update arrives.
_INSTANCE_LOCK = None
MUTEX_NAME = 'Global' + chr(92) + 'TeamSync-SingleInstance'


def claim_instance_lock():
    """True if we are the only copy. False means another one already owns it."""
    global _INSTANCE_LOCK
    ERROR_ALREADY_EXISTS = 183
    try:
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True                     # cannot tell; do not block the user
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        _INSTANCE_LOCK = handle
        return True
    except Exception:
        return True


def release_instance_lock():
    """Hand the lock over before starting our replacement during an update."""
    global _INSTANCE_LOCK
    if _INSTANCE_LOCK:
        try:
            ctypes.windll.kernel32.CloseHandle(_INSTANCE_LOCK)
        except Exception:
            pass
        _INSTANCE_LOCK = None


def raise_existing_window():
    """Bring the copy that is already running to the front."""
    try:
        SW_RESTORE = 9
        hwnd = ctypes.windll.user32.FindWindowW(None, APP_NAME)
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False

# ------------------------------------------------------------ explaining ----
# Every failure the user can see goes through here. An error is only useful if it
# says three things: what was being attempted, what went wrong, and what to do
# next. A bare exception message says none of them.

def explain(what, why, howto, kind="error", parent=None):
    body = latin(what + NN + why + NN + "What to do" + N + howto)
    show = {"error": messagebox.showerror,
            "warn": messagebox.showwarning,
            "info": messagebox.showinfo}[kind]
    try:
        show(APP_NAME, body, parent=parent) if parent else show(APP_NAME, body)
    except Exception:
        pass


def exe_home_writable():
    """Can we replace our own file where we are? Updates need that."""
    if not getattr(sys, "frozen", False):
        return True
    folder = os.path.dirname(sys.executable)
    probe = os.path.join(folder, ".teamsync-write-test")
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def preferred_home():
    """A folder we can always write to, whatever the machine is like."""
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(root, "Programs", APP_NAME)


def relocate_self():
    """Copy ourselves somewhere writable and continue from there.

    Where the exe happens to sit should not decide whether it works. If it landed
    somewhere it cannot update itself - Program Files, a read-only share, a synced
    folder - move into the user's own programs folder and carry on from there.
    """
    import shutil
    target_dir = preferred_home()
    target = os.path.join(target_dir, os.path.basename(sys.executable))
    try:
        os.makedirs(target_dir, exist_ok=True)
        if os.path.normcase(target) == os.path.normcase(sys.executable):
            return None
        shutil.copy2(sys.executable, target)
        subprocess.Popen([target], creationflags=CREATE_NO_WINDOW)
        return target
    except Exception:
        return None



def repo_slug(path):
    """owner/name as GitHub knows it, or "" when it cannot be read."""
    url = git(path, 'remote', 'get-url', 'origin')
    if not url:
        return ''
    return url.rstrip('/').removesuffix('.git').split('github.com')[-1].strip(':/')


def remember_project(cfg, path):
    """Keep a list of what has been opened, so opening again is a choice, not a hunt."""
    path = os.path.normpath(path)
    known = cfg.setdefault('projects', [])
    for entry in known:
        if os.path.normcase(entry.get('path', '')) == os.path.normcase(path):
            entry['name'] = repo_slug(path) or entry.get('name', '')
            return
    known.append({'path': path, 'name': repo_slug(path)})


def forget_project(cfg, path):
    path = os.path.normcase(os.path.normpath(path))
    cfg['projects'] = [e for e in cfg.get('projects', [])
                       if os.path.normcase(e.get('path', '')) != path]

# ------------------------------------------------------------- self-update ---


def _version_tuple(text):
    parts = []
    for chunk in str(text).lstrip("vV").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def gh(*args):
    """Run one gh command. Returns stdout stripped, or None if it failed."""
    try:
        out = subprocess.run(["gh"] + list(args), capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             creationflags=CREATE_NO_WINDOW)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


_release_etag = {"tag": None, "etag": None, "client": None, "tried": False}


def _release_client():
    """One authenticated HTTP client, built lazily from the gh token."""
    if _release_etag["tried"]:
        return _release_etag["client"]
    _release_etag["tried"] = True
    token = gh("auth", "token")
    if token:
        try:
            import urllib.request
            opener = urllib.request.build_opener()
            opener.addheaders = [("Authorization", f"Bearer {token}"),
                                 ("User-Agent", "teamsync"),
                                 ("Accept", "application/vnd.github+json")]
            _release_etag["client"] = opener
        except Exception:
            _release_etag["client"] = None
    return _release_etag["client"]


def latest_release():
    """Newest published version in the distribution repo, or None.

    Uses a conditional request: when nothing has been released since the last
    look, GitHub answers 304, which is fast and does not count against the API
    rate limit at all. That is why this can be asked every few seconds instead of
    every ten minutes - a new build reaches both machines almost at once.
    """
    opener = _release_client()
    if opener is None:
        tag = gh("release", "view", "--repo", UPDATE_REPO, "--json", "tagName", "-q", ".tagName")
        return {"tag": tag, "size": 0, "date": ""} if tag else None
    import urllib.request
    import urllib.error
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest")
    if _release_etag["etag"]:
        req.add_header("If-None-Match", _release_etag["etag"])
    try:
        with opener.open(req, timeout=15) as res:
            _release_etag["etag"] = res.headers.get("ETag")
            data = json.loads(res.read().decode("utf-8", "replace"))
            by_name = {a.get("name"): a for a in data.get("assets", [])}
            exe = by_name.get(PACKAGE_NAME, {})
            sig = by_name.get(PACKAGE_NAME + ".sig", {})
            _release_etag["tag"] = {
                "tag": data.get("tag_name"),
                "size": exe.get("size", 0),
                "date": (data.get("published_at") or "")[:10],
                "exe_id": exe.get("id"),
                "sig_id": sig.get("id"),
            }
            return _release_etag["tag"]
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return _release_etag["tag"]      # unchanged since last time
        return None
    except Exception:
        return None


def download_release(tag, destination, pattern="TeamSync.zip"):
    """Fetch one asset of that release with gh, when the direct path is unavailable."""
    out = subprocess.run(
        ["gh", "release", "download", tag, "--repo", UPDATE_REPO,
         "--pattern", pattern, "--output", destination, "--clobber"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return out.returncode == 0 and os.path.exists(destination) and os.path.getsize(destination) > 0



def verify_release(exe_path, sig_path):
    """Is this build signed by the key we trust? Anything else is not run.

    Without this, whoever can publish a release - or whoever steals the account
    that can - executes code on the other person's machine, silently. The check
    is the whole reason the update can stay automatic.
    """
    if not (os.path.exists(exe_path) and os.path.exists(sig_path)):
        return False, 'the signature file is missing'
    allowed = os.path.join(os.path.dirname(config_path()), 'allowed_signers')
    try:
        with open(allowed, 'w', encoding='utf-8') as fh:
            fh.write(SIGN_NAMESPACE + ' ' + RELEASE_PUBKEY + chr(10))
    except OSError as exc:
        return False, 'could not write the key file: ' + str(exc)
    try:
        with open(exe_path, 'rb') as payload:
            r = subprocess.run(
                ['ssh-keygen', '-Y', 'verify', '-f', allowed, '-I', SIGN_NAMESPACE,
                 '-n', SIGN_NAMESPACE, '-s', sig_path],
                stdin=payload, capture_output=True, text=True,
                creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return False, 'ssh-keygen is not available on this machine'
    except Exception as exc:
        return False, str(exc)
    if r.returncode == 0:
        return True, ''
    return False, (r.stderr or r.stdout or 'signature did not match').strip().splitlines()[0]


def download_signature(tag, destination):
    out = subprocess.run(
        ['gh', 'release', 'download', tag, '--repo', UPDATE_REPO,
         '--pattern', PACKAGE_NAME + '.sig', '--output', destination, '--clobber'],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW)
    return out.returncode == 0 and os.path.exists(destination)


def scan_with_antivirus(path):
    """Ask the machine's own antivirus to look at the file before we run it.

    A signature proves who built the file, not that the file is harmless - if the
    signer's own machine were compromised, the signature would still be valid. So
    the last step before running anything is the scanner the user already trusts.
    Returns (verdict, detail): 'clean', 'infected', or 'unknown' when no scanner
    answered - and 'unknown' is reported, never silently treated as clean.
    """
    mp = os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                      'Windows Defender', 'MpCmdRun.exe')
    if os.path.exists(mp):
        try:
            r = subprocess.run([mp, '-Scan', '-ScanType', '3', '-File', path, '-DisableRemediation'],
                               capture_output=True, text=True, timeout=180,
                               creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or '') + (r.stderr or '')
            if 'found no threats' in out.lower() or r.returncode == 0:
                return 'clean', 'Windows Defender found no threats'
            if 'threat' in out.lower() or 'malware' in out.lower():
                return 'infected', out.strip().splitlines()[-1] if out.strip() else 'a threat was reported'
            return 'unknown', 'the scanner gave no clear answer'
        except subprocess.TimeoutExpired:
            return 'unknown', 'the scan did not finish in time'
        except Exception as exc:
            return 'unknown', str(exc)
    return 'unknown', 'no antivirus command was found on this machine'


def download_asset(asset_id, destination, total, on_progress=None):
    """Fetch one release asset, reporting progress as it goes.

    gh can download this in one line but says nothing while it works, which on a
    slow VPN looks like the app has frozen. Streaming it ourselves costs a few
    lines and lets the window show what is happening.
    """
    opener = _release_client()
    if opener is None or not asset_id:
        return False
    import urllib.request
    req = urllib.request.Request(
        f'https://api.github.com/repos/{UPDATE_REPO}/releases/assets/{asset_id}')
    req.add_header('Accept', 'application/octet-stream')
    try:
        with opener.open(req, timeout=60) as res, open(destination, 'wb') as out:
            got = 0
            while True:
                chunk = res.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total or 0)
        return os.path.getsize(destination) > 0
    except Exception:
        try:
            if os.path.exists(destination):
                os.remove(destination)
        except OSError:
            pass
        return False

def app_dir():
    """The folder the program lives in, which is what an update replaces."""
    return os.path.dirname(os.path.abspath(sys.executable))


def staging_dir():
    root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(root, APP_NAME, "staging")
    os.makedirs(path, exist_ok=True)
    return path


def unpack_update(zip_path):
    """Extract the downloaded package next to nothing important. Returns its path."""
    import shutil
    import zipfile
    target = os.path.join(staging_dir(), "new")
    try:
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target)
        # A zip made from the folder itself has no wrapper; one made from its
        # parent has exactly one. Accept both.
        entries = os.listdir(target)
        if len(entries) == 1 and os.path.isdir(os.path.join(target, entries[0])):
            inner = os.path.join(target, entries[0])
            if os.path.exists(os.path.join(inner, "TeamSync.exe")):
                target = inner
        return target if os.path.exists(os.path.join(target, "TeamSync.exe")) else None
    except Exception:
        return None


def apply_update(unpacked):
    """Hand the swap to a helper, because we cannot replace the folder we run from.

    Windows will not let a directory be replaced while any process holds it -
    and "holds" includes having it as a current working directory. An app opened
    by double-click (or by the desktop shortcut) has the install folder as its
    cwd, and a child spawned without an explicit cwd inherits that. So the first
    shipped helper held the very folder it was renaming and failed every time,
    silently. Hence the two escapes below: the app leaves the folder before
    spawning, and the helper is both started in - and moves itself to - a
    neutral directory.
    """
    here = app_dir()
    staging = staging_dir()
    helper = os.path.join(staging, "apply-update.ps1")
    log = os.path.join(staging, "apply-update.log")
    script = chr(10).join([
        "param([int]$AppPid, [string]$Here, [string]$New, [string]$Log)",
        "$ErrorActionPreference = 'Continue'",
        "Set-Location -LiteralPath $env:TEMP    # never stand inside the folder being swapped",
        "function Note($m) { \"$(Get-Date -Format o)  $m\" | Add-Content -LiteralPath $Log }",
        "function Fail($why) {",
        "    Note \"FAILED: $why\"",
        "    # The old folder is intact (or restored), so give the user their app",
        "    # back instead of a closed window and silence. The restarted app",
        "    # reads this log and says what went wrong.",
        "    $exe = Join-Path $Here 'TeamSync.exe'",
        "    if (Test-Path -LiteralPath $exe) { Start-Process -FilePath $exe -WorkingDirectory $env:TEMP }",
        "    exit 1",
        "}",
        "Note \"waiting for $AppPid to exit\"",
        "for ($i = 0; $i -lt 240; $i++) {",
        "    if (-not (Get-Process -Id $AppPid -ErrorAction SilentlyContinue)) { break }",
        "    Start-Sleep -Milliseconds 500",
        "}",
        "if (Get-Process -Id $AppPid -ErrorAction SilentlyContinue) { Fail 'the app never exited' }",
        "Start-Sleep -Seconds 1",
        "$backup = $Here + '.previous'",
        "if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }",
        "$moved = $false",
        "for ($i = 0; $i -lt 60; $i++) {",
        "    # A freshly-installed tree can be held for tens of seconds by the",
        "    # antivirus scanning a first-run exe - outwait that. A PERMISSION",
        "    # refusal is a different animal: it never changes, so waiting on it",
        "    # only delays the honest answer by a minute.",
        "    try { Rename-Item -LiteralPath $Here -NewName ([IO.Path]::GetFileName($backup)) -ErrorAction Stop; $moved = $true; break }",
        "    catch {",
        "        if ($_.Exception -is [UnauthorizedAccessException] -or $_.Exception.Message -match 'denied') {",
        "            Fail ('Windows refused permission to change this folder. TeamSync is installed in a protected place (such as Program Files) where it cannot update itself. Move the whole TeamSync folder somewhere normal, for example C:' + [char]92 + 'Apps' + [char]92 + 'TeamSync, and updates will work.')",
        "        }",
        "        if ($i % 10 -eq 0) { Note ('still held after ' + $i + ' s: ' + $_.Exception.Message) }",
        "        Start-Sleep -Seconds 1",
        "    }",
        "}",
        "if (-not $moved) { Fail 'the old folder could not be moved aside - something still holds it' }",
        "try { Move-Item -LiteralPath $New -Destination $Here -ErrorAction Stop }",
        "catch {",
        "    # Move-Item cannot carry a directory across volumes (staging lives on",
        "    # C:, the app may not). Copying can.",
        "    try {",
        "        Copy-Item -LiteralPath $New -Destination $Here -Recurse -Force -ErrorAction Stop",
        "        Remove-Item -LiteralPath $New -Recurse -Force -ErrorAction SilentlyContinue",
        "    } catch {",
        "        Note \"putting the old one back: $_\"",
        "        Rename-Item -LiteralPath $backup -NewName ([IO.Path]::GetFileName($Here))",
        "        Fail \"the new folder could not be put in place: $_\"",
        "    }",
        "}",
        "Note 'new folder in place'",
        "Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue",
        "Start-Process -FilePath (Join-Path $Here 'TeamSync.exe') -WorkingDirectory $env:TEMP",
        "Note 'started the new version'",
    ])
    try:
        with open(helper, "w", encoding="utf-8") as fh:
            fh.write(script)
    except OSError:
        return False

    try:
        os.chdir(staging)            # release our own hold on the install folder
    except OSError:
        pass
    release_instance_lock()          # the replacement needs to be able to claim it
    try:
        subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper,
             "-AppPid", str(os.getpid()), "-Here", here, "-New", unpacked, "-Log", log],
            cwd=staging, creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def install_dir_updatable():
    """Can an unelevated update actually replace this installation?

    Writing a scrap file where the app lives is the same permission the helper
    needs to move the folder. Checking here, before anything downloads, turns a
    ten-megabyte download plus a minute of retrying plus a generic failure into
    an immediate message that names the real problem - the friend's install in
    Program Files spent all three to learn what this finds in a millisecond.
    """
    probe = os.path.join(app_dir(), ".ts-write-probe")
    try:
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def report_failed_update():
    """The reason the last update failed, or None.

    The helper cannot show a window, so a failed swap used to be pure silence:
    the app closed, nothing reopened, and the user learned nothing. The helper
    now leaves a FAILED line in its log and restarts the old app; this reads
    that line once, so the restarted app can say what actually happened.
    """
    log = os.path.join(staging_dir(), "apply-update.log")
    try:
        import time
        if time.time() - os.path.getmtime(log) > 1800:
            return None                      # stale: some earlier story, not this launch
        lines = [l.strip() for l in open(log, encoding="utf-8", errors="replace") if l.strip()]
        if not lines or "FAILED" not in lines[-1]:
            return None
        reason = lines[-1].split("FAILED:", 1)[-1].strip()
        # Tell the story once, but keep the evidence: the deleted log of the
        # first field failure left nothing to diagnose from.
        kept = log[:-4] + ".last.log"
        try:
            if os.path.exists(kept):
                os.remove(kept)
            os.rename(log, kept)
        except OSError:
            try:
                os.remove(log)
            except OSError:
                pass
        return reason
    except OSError:
        return None


def clean_old_exe():
    """Remove what the previous update left behind, once we are safely running."""
    import shutil
    for leftover in (app_dir() + ".previous", os.path.join(staging_dir(), "new")):
        try:
            if os.path.isdir(leftover):
                shutil.rmtree(leftover, ignore_errors=True)
        except Exception:
            pass


SHORTCUT_SCRIPT = "$sh = New-Object -ComObject WScript.Shell\n$s = $sh.CreateShortcut($env:TS_LINK)\n$set = $false\nforeach ($pair in @(@($env:TS_TARGET, $env:TS_WORKDIR), @($env:TS_TARGET_ALT, $env:TS_WORKDIR_ALT))) {\n    if (-not $pair[0]) { continue }\n    try { $s.TargetPath = $pair[0]; $s.WorkingDirectory = $pair[1]; $set = $true; break } catch { }\n}\nif (-not $set) { Write-Error 'Windows would not accept the path of the program.'; exit 1 }\nif ($env:TS_ICON) { try { $s.IconLocation = $env:TS_ICON } catch { } }\n$s.Description = 'TeamSync'\n$s.Save()\n# Saving can succeed and still leave a shortcut that points nowhere, so read\n# it back before calling this done.\nif (-not $sh.CreateShortcut($env:TS_LINK).TargetPath) {\n    Write-Error 'The shortcut was written but points at nothing.'; exit 2\n}"

def desktop_dir():
    """Where this account's desktop really is.

    Not always under the home folder: OneDrive redirects it, and on a machine
    where it has been redirected, writing to ~/Desktop puts the shortcut
    somewhere the user never looks.
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as k:
            path = os.path.expandvars(winreg.QueryValueEx(k, "Desktop")[0])
            if os.path.isdir(path):
                return path
    except OSError:
        pass
    fallback = os.path.join(os.path.expanduser("~"), "Desktop")
    return fallback if os.path.isdir(fallback) else None


def desktop_shortcut_exists(cfg):
    """True when this installation has already put a shortcut on the desktop.

    It asks what the app remembers doing, not what the .lnk contains. A shortcut
    file stores its target in the system codepage, so on a machine whose folder
    names cannot be written in that codepage - a path with Persian in it, for
    instance - the target is simply not in the file to be found. Asking Windows
    to resolve it instead costs a subprocess on every launch.

    Fails safe: if the record is missing or the file has been deleted, the button
    comes back, and pressing it again is harmless.
    """
    link = cfg.get("desktop_shortcut")
    if not link or not os.path.exists(link):
        return False
    return cfg.get("desktop_shortcut_for") == os.path.abspath(sys.executable)


def short_path(path):
    """The old 8.3 form of a path, which is always plain ascii.

    Needed because the shell object that makes shortcuts refuses a target whose
    path contains characters outside the system codepage - a folder named in
    Persian, for instance. It answers "Value does not fall within the expected
    range" and then saves a shortcut with no target at all, which Windows draws
    as a blank icon that opens nothing. The short form sidesteps that, and
    Windows still resolves it back to the real name.
    """
    try:
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return ""


def create_desktop_shortcut():
    """Put a shortcut on the desktop. Returns (ok, where-or-why).

    This exists because the obvious move is wrong. The program is a folder now,
    and dragging TeamSync.exe out of it onto the desktop produces an exe that
    cannot start - it has been separated from everything it needs. A shortcut
    points at the exe where it lives, and keeps working after an update, because
    an update puts the new folder at the same path.
    """
    if not getattr(sys, "frozen", False):
        return False, "This only works in the built app, not when run from source."
    desktop = desktop_dir()
    if not desktop:
        return False, "Your Desktop folder could not be found."
    link = os.path.join(desktop, APP_NAME + ".lnk")
    exe, here = os.path.abspath(sys.executable), app_dir()
    # The three paths travel in the environment rather than on the command line.
    # PowerShell's -Command does not bind positional arguments to $args - that is
    # -File behaviour - so anything passed after the script is appended to the
    # script text itself and read as code.
    ico = resource_path("assets", "teamsync.ico")
    ico_spec = ""
    if os.path.exists(ico):
        safe = short_path(ico) or (ico if ico.isascii() else "")
        if safe:
            ico_spec = safe + ",0"
    env = dict(os.environ,
               TS_LINK=link, TS_TARGET=exe, TS_WORKDIR=here, TS_ICON=ico_spec,
               TS_TARGET_ALT=short_path(exe), TS_WORKDIR_ALT=short_path(here))
    ps = SHORTCUT_SCRIPT
    try:
        out = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=30, env=env,
            creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        return False, str(exc)
    if out.returncode == 0 and os.path.exists(link):
        return True, link
    try:
        if os.path.exists(link):
            os.remove(link)      # a shortcut that opens nothing is worse than none
    except OSError:
        pass
    return False, (out.stderr or out.stdout or "Windows refused to create the shortcut.").strip()


def install_editor_extension():
    """Plant the editor-presence extension where VS Code loads extensions from.

    The extension is TeamSync's eyes inside the editor: which project files
    are open, which carry unsaved typing, and the moment they close - facts
    Windows itself keeps no record of. VS Code scans this folder when it
    starts, so a fresh copy is picked up at the editor's next restart. With no
    VS Code on the machine the folder simply sits unread; nothing breaks.
    """
    src = resource_path("editor-extension")
    if not os.path.isdir(src):
        return
    home = os.path.expanduser("~")
    # Every VS Code flavour keeps its own extensions folder. Plant into each
    # one whose parent exists - an existing parent means that editor is (or
    # was) installed; nothing is created for editors that are not there.
    for flavour in (".vscode", ".vscode-insiders", ".vscode-oss", ".cursor", ".windsurf"):
        base = os.path.join(home, flavour)
        if not os.path.isdir(base):
            continue
        dst = os.path.join(base, "extensions", "teamsync.presence-1.0.0")
        try:
            for name in os.listdir(src):
                body = open(os.path.join(src, name), "rb").read()
                target = os.path.join(dst, name)
                if not os.path.exists(target) or open(target, "rb").read() != body:
                    os.makedirs(dst, exist_ok=True)
                    with open(target, "wb") as fh:
                        fh.write(body)
        except OSError:
            continue


def refresh_desktop_shortcut(cfg):
    """Point the desktop shortcut's icon at the current art after an update.

    Windows caches a shortcut's icon by the path it draws it from, and our
    path deliberately never changes across updates - so when the art inside
    the exe changes, the desktop keeps showing the old picture forever. The
    cure is threefold: the .lnk names its icon explicitly, it is rewritten
    once per new version, and SHChangeNotify tells Explorer that what it has
    cached is stale.
    """
    if not desktop_shortcut_exists(cfg):
        return
    if cfg.get("shortcut_icon_for") == APP_VERSION:
        return                       # already renewed for this version
    ok, _ = create_desktop_shortcut()
    if ok:
        cfg["shortcut_icon_for"] = APP_VERSION
        save_config(cfg)
        try:                         # SHCNE_ASSOCCHANGED: "re-read your icons"
            ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
        except Exception:
            pass


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(on):
    """Register/unregister this exe to start at Windows login. Exe builds only."""
    import winreg
    if on and not getattr(sys, "frozen", False):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --autostart')
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except OSError:
                    pass
        return True
    except OSError:
        return False



# Windows can be told to draw digits in the local script. On a Persian setup that
# turns 'v1.3.1' into Persian numerals inside an otherwise English window. The
# substitution follows the surrounding text direction, so opening a string with a
# LEFT-TO-RIGHT MARK pins it to latin shapes wherever the app happens to run.
LRM = '\u200e'


def latin(text):
    """Show digits in latin shapes regardless of the machine's regional setting."""
    text = str(text)
    return text if text.startswith(LRM) else LRM + text


# ------------------------------------------------------------------ theme ---

BG      = "#12141a"
PANEL   = "#1a1d26"
FG      = "#e6e8ee"
MUTED   = "#8b93a7"
ACCENT  = "#4f8cff"
OK      = "#39d98a"
WARN    = "#f5b544"
BAD     = "#ff5d5d"
FONT    = ("Segoe UI", 10)
FONT_B  = ("Segoe UI Semibold", 10)
FONT_H  = ("Segoe UI Semibold", 14)
MONO    = ("Consolas", 9)

# Persian help text for every button. Native Windows message boxes render
# right-to-left text correctly, so this needs no special handling.
HELP = {
    'start_new': (
        "Start a new shared project.\n\n"
        "You pick a folder that already holds your work. That folder and everything under it becomes a PRIVATE repository on your GitHub account, your teammate is invited, and syncing starts.\n\n"
        "Once per project. The repository name must be plain lower-case latin - if the folder name is not, type one yourself."
    ),
    'join': (
        "Join a project someone shared with you.\n\n"
        "Type the repository name they gave you. It downloads into any empty folder you choose - anywhere on your disk. A short latin path without spaces gives the fewest surprises.\n\n"
        "Accept the GitHub invitation first. Until you do, GitHub answers \"repository not found\", which looks like a typo but is not."
    ),
    'open_existing': (
        "Open a project already on this machine.\n\n"
        "For later days, or for moving between several shared projects. Pick the project folder itself - the one containing .git - not the folder above it.\n\n"
        "You rarely need this: the app reopens your last project on its own."
    ),
    'publish': (
        "Publish now.\n\n"
        "Commits what you have and sends it, so the other person has it within seconds.\n\n"
        "Nothing is lost without it: work goes out automatically after 4 quiet minutes. This button is for when a piece is finished and you would rather not wait. AI agents press the same button through push-now.ps1.\n\n"
        "It also clears the warning your teammate sees on the files you were editing."
    ),
    'syncbtn': (
        "Turn syncing on or off.\n\n"
        "Off means nothing is sent and nothing is received until you turn it on again. Your work stays safe in local commits either way.\n\n"
        "Closing the window does NOT turn syncing off - the engine keeps running in the background. This button and Disconnect are the only off switches."
    ),
    'openfolder': (
        "Open the project folder in Windows Explorer."
    ),
    'relocate': (
        "Change folder.\n\n"
        "If you moved or renamed the project folder, point the app at its new location from here.\n\n"
        "No need to close the window or use Switch. Syncing stops at the old path and starts at the new one. Nothing is lost."
    ),
    'switch': (
        "Switch project.\n\n"
        "Returns to the first screen so you can open a different project. Syncing of the current project is NOT stopped - it carries on in the background."
    ),
    'disconnect': (
        "Disconnect this project.\n\n"
        "Stops its sync engine completely - nothing left running in the background - and removes the project from the app's list.\n\n"
        "Nothing is deleted: the files on disk and the GitHub repository are untouched. Reconnect any time with \"Open a project\"."
    ),
    'partner': (
        "Teammate light.\n\n"
        "Says whether the other side is syncing right now.\n\n"
        "Green means their engine is running and its heartbeat is fresh. Amber means they are offline, with how long ago they were last seen. Grey means they have never connected - most likely the GitHub invitation is still unaccepted.\n\n"
        "The heartbeat travels through the repository itself and adds no commits to your project history. It refreshes every 60 seconds, so up to a minute of lag is normal."
    ),
    'shortcut': (
        "Add to desktop.\n\n"
        "Puts a shortcut on your desktop that opens TeamSync.\n\n"
        "Use this rather than dragging TeamSync.exe there yourself. The program is "
        "a folder, and the exe taken out of that folder cannot start - it has been "
        "separated from the files it runs on.\n\n"
        "The shortcut survives updates: a new version is put at the same place, so "
        "the shortcut keeps pointing at it."
    ),
    'history': (
        "Activity history.\n\n"
        "The live log keeps roughly today and never fewer than the last hundred "
        "lines, so it stays readable. Everything older is moved into a history "
        "file kept next to the project - this button opens it.\n\n"
        "History is local to this machine and never synced."
    ),
    'autostart': (
        "Start with Windows.\n\n"
        "When on, TeamSync opens itself after every login and carries on syncing your last project.\n\n"
        "Nothing runs while the machine is off, but nothing is lost either: unpublished work waits in local commits and both sides line up again once you are back."
    ),
}


class Pill(ttk.Frame):
    """A status light: a coloured dot and a label, on the themed surface."""

    def __init__(self, parent):
        super().__init__(parent)
        self.dot = ttk.Label(self, text="●", foreground=MUTED,
                             font=("Segoe UI", 11))
        self.dot.pack(side="left", padx=(0, 10))
        self.lbl = ttk.Label(self, text="idle", foreground=MUTED, font=FONT_B)
        self.lbl.pack(side="left")

    def set(self, text, color):
        self.dot.configure(foreground=color)
        self.lbl.configure(text=latin(text), foreground=color)


def _shade(colour, factor):
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb colour."""
    colour = colour.lstrip("#")
    rgb = [int(colour[i:i + 2], 16) for i in (0, 2, 4)]
    rgb = [max(0, min(255, int(c * factor))) for c in rgb]
    return "#%02x%02x%02x" % tuple(rgb)


def button(parent, text, command, primary=False, danger=False):
    """A real Windows 11 button rather than something drawn by hand.

    Sun Valley gives these proper hover, press, focus and keyboard behaviour,
    which a canvas drawing can imitate but never actually have.
    """
    style = "Accent.TButton" if primary else "TButton"
    b = ttk.Button(parent, text=text, command=command, style=style)
    if danger:
        b.configure(style="Danger.TButton")
    return b


def help_button(parent, key):
    """The small ? beside a control.

    A themed button carries generous padding by default, which next to a status
    light is taller than the line it is explaining. Small.TButton strips that back.
    """
    # Returned inside a holder so the gap travels with the button and every
    # caller gets the same spacing without remembering to ask for it.
    holder = ttk.Frame(parent)
    ttk.Button(holder, text="?", width=2, style="Small.TButton",
               command=lambda: messagebox.showinfo(APP_NAME, HELP[key])).pack(padx=(8, 0))
    return holder


# ------------------------------------------------------------- setup forms ---

class SetupDialog(tk.Toplevel):
    """Collects what init-owner.ps1 / init-friend.ps1 need."""

    def __init__(self, parent, mode, cfg):
        super().__init__(parent)
        self.mode = mode
        self.result = None
        self.title("Start a new shared project" if mode == "owner" else "Join a project")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        wrap = ttk.Frame(self, padding=(22, 18))
        wrap.pack(fill="both", expand=True)

        head = "Turn a folder into a shared private project" if mode == "owner" \
               else "Connect to a project someone shared with you"
        ttk.Label(wrap, text=head, font=FONT_H, foreground=FG).pack(anchor="w")

        note = ("Everything in the folder, including sub-folders, is uploaded to a\n"
                "new PRIVATE repository on your GitHub account."
                if mode == "owner" else
                "Accept the GitHub invitation first, otherwise this fails with\n"
                "\"repository not found\" - which is not a typo on your side.")
        ttk.Label(wrap, text=note, font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 14))

        self.vars = {}
        if mode == "owner":
            self._folder_row(wrap, "path", "Project folder", pick_existing=True)
            self._row(wrap, "reponame", "Repository name", "",
                      "Lower-case latin only. Leave empty to use the folder name.")
            self._row(wrap, "friend", "Their GitHub username", cfg.get("friend", ""))
        else:
            self._row(wrap, "reponame", "Repository name", "", "The name they gave you.")
            self._row(wrap, "owner", "Their GitHub username", cfg.get("owner", "aminbm1919"))
            self._folder_row(wrap, "path", "Download into (any empty folder you like)",
                             pick_existing=False)

        self._row(wrap, "me", "Your name", cfg.get("me", ""), "Shown as the author of your commits.")
        self._row(wrap, "email", "Your GitHub email", cfg.get("email", ""))

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(16, 0))
        button(row, "Cancel", self.destroy).pack(side="right")
        button(row, "Start" if mode == "owner" else "Connect", self._ok, primary=True).pack(side="right", padx=(0, 8))

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 70
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _row(self, parent, key, label, default="", hint=""):
        ttk.Label(parent, text=label, font=FONT_B, foreground=FG).pack(anchor="w", pady=(8, 2))
        var = tk.StringVar(value=default)
        ttk.Entry(parent, textvariable=var, font=FONT, width=52).pack(anchor="w", ipady=6, fill="x")
        if hint:
            ttk.Label(parent, text=hint, font=("Segoe UI", 8), foreground=MUTED).pack(anchor="w")
        self.vars[key] = var

    def _folder_row(self, parent, key, label, pick_existing):
        ttk.Label(parent, text=label, font=FONT_B, foreground=FG).pack(anchor="w", pady=(8, 2))
        holder = ttk.Frame(parent)
        holder.pack(fill="x")
        var = tk.StringVar(value="")
        ttk.Entry(holder, textvariable=var, font=FONT).pack(side="left", fill="x", expand=True, ipady=6)

        def pick():
            p = filedialog.askdirectory(title=label, mustexist=pick_existing)
            if p:
                var.set(os.path.normpath(p))

        button(holder, "Browse", pick).pack(side="left", padx=(8, 0))
        self.vars[key] = var

    def _ok(self):
        vals = {k: v.get().strip() for k, v in self.vars.items()}
        if not vals.get("path"):
            messagebox.showwarning(APP_NAME, "Choose a folder first.", parent=self)
            return
        if self.mode == "friend" and not vals.get("reponame"):
            messagebox.showwarning(APP_NAME, "Enter the repository name they gave you.", parent=self)
            return
        self.result = vals
        self.destroy()


class ProjectChooser(tk.Toplevel):
    """Pick from what has been opened before, instead of hunting for a folder.

    Built on a Treeview rather than hand-made rows: selection, keyboard movement
    and double-click come with the widget. The first attempt drew its own rows
    and tracked selection by repainting their background - which cannot work on
    themed widgets, because they have no background to set, so clicking a row did
    nothing at all.
    """

    def __init__(self, parent, projects):
        super().__init__(parent)
        self.result = None
        self.projects = list(projects)
        self.title("Open a project")
        self.transient(parent)
        self.grab_set()
        self.minsize(620, 300)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Projects on this machine", font=FONT_H).pack(anchor="w")
        hint = ("Double-click one, or select it and press Open."
                if self.projects else "Nothing has been opened on this machine yet.")
        ttk.Label(wrap, text=hint, foreground=MUTED).pack(anchor="w", pady=(2, 12))

        holder = ttk.Frame(wrap)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("path",), show="tree headings",
                                 selectmode="browse", height=8)
        self.tree.heading("#0", text="Project")
        self.tree.heading("path", text="Folder")
        self.tree.column("#0", width=210, stretch=False)
        self.tree.column("path", width=380)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.tag_configure("missing", foreground=BAD)

        for entry in self.projects:
            path = entry.get("path", "")
            here = os.path.isdir(os.path.join(path, ".git"))
            name = entry.get("name") or os.path.basename(path.rstrip(chr(92) + "/")) or path
            if not here:
                name += "   (folder is missing)"
            self.tree.insert("", "end", text=name, values=(path,),
                             tags=() if here else ("missing",))
        if self.projects:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)

        self.tree.bind("<Double-Button-1>", lambda e: self._open())
        self.tree.bind("<Return>", lambda e: self._open())

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(16, 0))
        button(row, "Cancel", self.destroy).pack(side="right")
        button(row, "Open", self._open, primary=True).pack(side="right", padx=(0, 8))
        button(row, "Browse for a folder...", self._browse).pack(side="left")
        button(row, "Remove from list", self._forget, danger=True).pack(side="left", padx=(8, 0))

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 70
        self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))

    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a project first.", parent=self)
            return None
        return self.tree.item(sel[0], "values")[0]

    def _open(self):
        path = self._selected_path()
        if path:
            self.result = ("open", path)
            self.destroy()

    def _browse(self):
        self.result = ("browse", None)
        self.destroy()

    def _forget(self):
        path = self._selected_path()
        if path:
            self.result = ("forget", path)
            self.destroy()


# -------------------------------------------------------------- main window ---

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        # The icon compiled into the exe covers the taskbar and the shortcut; the
        # title bar is drawn by Tk and needs telling separately, or it keeps the
        # stock feather that has nothing to do with this program.
        try:
            self.iconbitmap(resource_path("assets", "teamsync.ico"))
        except Exception:
            pass
        self.geometry("920x640")
        self.minsize(800, 540)

        # The theme is decoration. It lives in a .tcl file that has to survive
        # being unpacked to a temp folder, and on a machine where antivirus
        # interferes with that, the file is simply not there. Losing the looks is
        # acceptable; refusing to start over a stylesheet is not.
        try:
            if sv_ttk:
                sv_ttk.set_theme("dark")
        except Exception as exc:
            self._theme_failed = str(exc)
        style = ttk.Style()
        # Two styles the theme does not ship: a destructive one, and a small
        # muted one for the "?" buttons so they do not compete with the actions.
        style.configure("Danger.TButton", foreground=BAD)
        # width=2 alone is not enough: the padding is what makes a themed button
        # tall. Cut it, and shrink the glyph so it sits inside the line it marks.
        style.configure("Small.TButton", padding=(0, 0), font=("Segoe UI", 8))

        self.cfg = load_config()
        self.repo = self.cfg.get("last_project") or ""
        self.proc = None            # engine started by THIS window (else adopted by pid)
        self.lines = queue.Queue()
        self.conflict_dir = None
        self._log_pos = 0
        self._update_ready = None      # path of a downloaded newer exe
        self._update_available = None  # {tag,size,date} seen but not taken
        self._update_busy = False
        clean_old_exe()
        refresh_desktop_shortcut(self.cfg)
        install_editor_extension()
        failure = report_failed_update()
        if failure:
            self.after(600, lambda: explain(
                'The last update could not be installed.',
                'The new version was downloaded and verified, but swapping it in '
                'failed: ' + failure,
                'Nothing is lost - this is still the old version, working '
                'normally. Press the update button to try again; if it keeps '
                'failing, tell Amin this exact message.', kind='warn'))
        self.after(1200, self._check_home)
        if getattr(self, "_theme_failed", None):
            self.lines.put("running with the plain look - the theme file could not "
                           "be read (" + self._theme_failed.split(":")[-1].strip() + ")")

        came_from = self.cfg.pop("updated_from", None)
        if came_from and came_from != APP_VERSION:
            # Close the loop for the user: the window that disappeared a second
            # ago did so on purpose, and here is the proof it worked.
            self.lines.put(f"updated from v{came_from} to v{APP_VERSION}")
            save_config(self.cfg)

        self._build()
        self.after(150, self._drain)
        self.after(700, self._tail_log)
        self.after(1500, self._poll_git)
        self.after(4000, self._update_tick)
        self.protocol("WM_DELETE_WINDOW", self._close)

        remembered = self.repo
        if remembered and os.path.isdir(os.path.join(remembered, ".git")):
            self._show_project()
            self.start_sync()
        else:
            self.repo = ""
            self._show_welcome()
            if remembered:
                # A moved or renamed folder is the usual cause, and silently
                # showing the welcome screen makes it look like the project was
                # lost. Nothing is lost - the repository travelled with the folder.
                self.missing_lbl.config(
                    text=f"The last project is not at this path any more:\n{remembered}\nIf you moved the folder, use \"Open a project already on this machine\" to point at its new location. Nothing has been lost.")
                self.missing.pack(fill="x", pady=(0, 18), before=self.welcome_actions)

    # -- layout ------------------------------------------------------------

    def _build(self):
        self.header = ttk.Frame(self, padding=(18, 12))
        self.header.pack(fill="x")

        # A strip along the very top of the window, level with the app's own name
        # in the title bar. Windows will not let a program put controls in that
        # bar itself without drawing the whole frame by hand, so this is the
        # nearest line inside the window.
        self.topbar = ttk.Frame(self.header)
        self.topbar.pack(fill="x", pady=(0, 10))
        ttk.Button(self.topbar, text="Help", width=8,
                   command=self._show_help).pack(side="left")
        self.shortcut_row = ttk.Frame(self.topbar)
        ttk.Button(self.shortcut_row, text="Add to desktop", width=15,
                   command=self._make_shortcut).pack(side="left")
        help_button(self.shortcut_row, "shortcut").pack(side="left")
        # Shown only when there is nothing on the desktop yet.
        if not desktop_shortcut_exists(self.cfg):
            self.shortcut_row.pack(side="left", padx=(24, 0))

        row = ttk.Frame(self.header)
        row.pack(fill="x")

        left = ttk.Frame(row)
        left.pack(side="left", fill="x", expand=True)
        # Without this line the big name below reads as the name of the program.
        # It is the project, which is a different thing and the one that matters.
        self.caption_lbl = ttk.Label(left, text="", font=("Segoe UI", 8), foreground=MUTED)
        self.caption_lbl.pack(anchor="w")
        self.title_lbl = ttk.Label(left, text=APP_NAME, font=FONT_H, foreground=FG)
        self.title_lbl.pack(anchor="w")
        self.path_lbl = ttk.Label(left, text="no project open", font=("Segoe UI", 9), foreground=MUTED)
        self.path_lbl.pack(anchor="w")

        self.update_btn = button(row, "", self._take_update, primary=True)
        # deliberately not packed yet - it appears only when there is something
        # to take, and stays there until it is taken

        right = ttk.Frame(row)
        right.pack(side="right")
        status_row = ttk.Frame(right)
        status_row.pack(anchor="e")
        self.pill = Pill(status_row)
        self.pill.pack(side="left")
        # A spacer the same width as a help button, so the two lights line up
        # even though only the lower one has something to explain.
        ttk.Label(status_row, text="  ", width=3).pack(side="left", padx=(8, 0))

        partner_row = ttk.Frame(right)
        partner_row.pack(anchor="e")
        self.partner_pill = Pill(partner_row)
        self.partner_pill.pack(side="left")
        self.partner_pill.set("partner: -", MUTED)
        help_button(partner_row, "partner").pack(side="left")

        # The live "working on" line. Not a log entry that scrolls away: it
        # stands here for as long as the partner has unpublished hands on
        # files, and vanishes the moment they publish.
        self.partner_files_lbl = ttk.Label(right, text="", font=("Segoe UI", 9, "bold"),
                                           foreground=WARN, justify="right")

        auto_row = ttk.Frame(right)
        auto_row.pack(anchor="e")
        self.version_lbl = ttk.Label(right, text=latin(f"v{APP_VERSION}"), font=("Segoe UI", 8), foreground=MUTED)
        self.version_lbl.pack(anchor="e")

        if autostart_enabled():
            set_autostart(True)      # rewrite the path in case the exe moved
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(auto_row, text="Start with Windows", variable=self.autostart_var, command=self._toggle_autostart).pack(side="left")
        help_button(auto_row, "autostart").pack(side="left")

        # A thin progress strip under the header. Hidden until something is
        # actually downloading, so it never adds noise to a quiet window.
        self.progress = ttk.Frame(self)
        self.progress_bar = tk.Canvas(self.progress, height=6, bg="#2a2f3d",
                                      highlightthickness=0)
        self.progress_bar.pack(fill="x", padx=18, pady=(0, 4))
        self.progress_lbl = ttk.Label(self.progress, text="", font=("Segoe UI", 8), foreground=MUTED, anchor="w")
        self.progress_lbl.pack(fill="x", padx=18, pady=(0, 6))
        self._progress_fill = self.progress_bar.create_rectangle(0, 0, 0, 6,
                                                                 fill=ACCENT, outline="")

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        # welcome
        self.welcome = ttk.Frame(self.body, padding=(40, 44))
        ttk.Label(self.welcome, text="Share a folder with one other person.",
                 font=("Segoe UI Semibold", 18), foreground=FG).pack(anchor="w")
        ttk.Label(self.welcome,
                 text=("Everything in the folder stays on both machines and on a private\n"
                       "GitHub repository. Work is published when you press Publish, and\n"
                       "automatically after four quiet minutes if you forget."),
                 font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(8, 26))

        self.missing = ttk.Frame(self.welcome, padding=(16, 12))
        self.missing_lbl = ttk.Label(self.missing, text="", font=FONT,
                                    foreground="#ffd7d7", justify="right", anchor="e")
        self.missing_lbl.pack(fill="x")
        button(self.missing, "Locate the folder", self._locate_missing,
               danger=True).pack(anchor="w", pady=(10, 0))

        self.welcome_actions = ttk.Frame(self.welcome)
        self.welcome_actions.pack(anchor="w", fill="x")

        for text, cmd, key, primary in (
            ("Start a new shared project", lambda: self._setup("owner"), "start_new", True),
            ("Join a project someone shared with me", lambda: self._setup("friend"), "join", False),
            ("Open a project already on this machine", self._open_existing, "open_existing", False),
        ):
            row = ttk.Frame(self.welcome_actions)
            row.pack(anchor="w", pady=4)
            button(row, text, cmd, primary=primary).pack(side="left")
            help_button(row, key).pack(side="left", padx=(6, 0))

        # project view
        self.project = ttk.Frame(self.body)

        bar = ttk.Frame(self.project, padding=(18, 14))
        bar.pack(fill="x")

        def action(parent, text, cmd, key, primary=False, danger=False, side="left"):
            grp = ttk.Frame(parent)
            grp.pack(side=side, padx=(0, 8) if side == "left" else (8, 0))
            b = button(grp, text, cmd, primary=primary, danger=danger)
            b.pack(side="left")
            help_button(grp, key).pack(side="left", padx=(3, 0))
            return b

        self.btn_push = action(bar, "Publish now", self.push_now, "publish", primary=True)
        self.btn_sync = action(bar, "Stop sync", self.toggle_sync, "syncbtn")
        action(bar, "Open folder", self._open_folder, "openfolder")
        action(bar, "Change folder", self._relocate, "relocate")
        action(bar, "History", self._show_history, "history")
        action(bar, "Disconnect", self._disconnect, "disconnect", danger=True, side="right")
        action(bar, "Switch project", self._switch, "switch", side="right")

        self.banner = ttk.Frame(self.project, padding=(18, 12))
        self.banner_lbl = ttk.Label(self.banner, text="", font=FONT_B,
                                   foreground="#ffd7d7", justify="left")
        self.banner_lbl.pack(side="left")
        button(self.banner, "Open both versions", self._open_conflict, danger=True).pack(side="right")

        logwrap = ttk.Frame(self.project, padding=(18, 0))
        logwrap.pack(fill="both", expand=True, pady=(0, 16))
        ttk.Label(logwrap, text="Activity", font=FONT_B, foreground=MUTED).pack(anchor="w", pady=(0, 6))
        self.log = tk.Text(logwrap, font=MONO, relief="flat", wrap="word",
                           padx=12, pady=10, borderwidth=0,
                           background=PANEL, foreground=FG, insertbackground=FG,
                           selectbackground="#31405e", selectforeground="#eaf0ff",
                           inactiveselectbackground="#31405e", exportselection=False)
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")
        # A disabled Text never takes keyboard focus on click, so a selection
        # is visible but Ctrl+C reaches some other widget and copies nothing.
        # Give it focus by hand and do the copy ourselves.
        self.log.bind("<Button-1>", lambda e: self.log.focus_set())
        # When a selection includes a line's trailing newline, Tk paints that
        # newline's highlight to the widget's right edge, so selected lines
        # read as one solid slab wider than the words. Strip just the newlines
        # from the selection whenever it changes; copying is unaffected
        # because the copy takes everything between the ends anyway.
        self._log_trim_busy = False
        self.log.bind("<<Selection>>", self._trim_log_selection)
        self.log.bind("<Control-c>", self._copy_log_selection)
        self.log.bind("<Control-a>", self._select_log_all)
        menu = tk.Menu(self.log, tearoff=0)
        menu.add_command(label="Copy", command=self._copy_log_selection)
        menu.add_command(label="Select all", command=self._select_log_all)
        self.log.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
        for tag, col in (("ok", OK), ("warn", WARN), ("bad", BAD), ("dim", MUTED)):
            self.log.tag_configure(tag, foreground=col)

    def _show_welcome(self):
        self.project.pack_forget()
        self.welcome.pack(fill="both", expand=True)
        self.caption_lbl.config(text="")
        self.title_lbl.config(text=APP_NAME)
        self.path_lbl.config(text="no project open")
        self.pill.set("idle", MUTED)

    def _show_project(self):
        self.missing.pack_forget()
        self.welcome.pack_forget()
        self.project.pack(fill="both", expand=True)
        # Remember it the moment it is opened, not only when it was set up. Then
        # closing the window on a project always reopens on that project.
        if self.repo:
            before = json.dumps(self.cfg.get("projects", []), sort_keys=True)
            remember_project(self.cfg, self.repo)
            if (self.cfg.get("last_project") != self.repo
                    or json.dumps(self.cfg.get("projects", []), sort_keys=True) != before):
                self.cfg["last_project"] = self.repo
                save_config(self.cfg)
        self.caption_lbl.config(text="Connected project - you are looking at:")
        self.title_lbl.config(text=os.path.basename(self.repo.rstrip("\\/")) or self.repo)
        self.path_lbl.config(text=self.repo)
        # show recent history, then tail only what is new
        logf = os.path.join(self.repo, ".teamsync.log")
        try:
            with open(logf, "r", encoding="utf-8", errors="replace") as fh:
                tail = fh.readlines()[-12:]
                self._log_pos = os.path.getsize(logf)
            for line in tail:
                self.lines.put(line.rstrip())
        except OSError:
            self._log_pos = 0

    # -- log ---------------------------------------------------------------

    def _trim_log_selection(self, event=None):
        if self._log_trim_busy:
            return                       # our own edits fire this event too
        ranges = self.log.tag_ranges("sel")
        if not ranges:
            return
        # tag_remove raises a fresh <<Selection>> even when it removes nothing,
        # so an unconditional pass here would chase its own tail forever. Only
        # touch the tag when some range actually spans a line break.
        pairs = list(zip(ranges[::2], ranges[1::2]))
        if all(str(self.log.index(a)).split(".")[0] == str(self.log.index(b)).split(".")[0]
               for a, b in pairs):
            return
        self._log_trim_busy = True
        try:
            first = int(str(self.log.index(ranges[0])).split(".")[0])
            last = int(str(self.log.index(ranges[-1])).split(".")[0])
            for ln in range(first, last + 1):
                self.log.tag_remove("sel", "%d.end" % ln, "%d.0" % (ln + 1))
        finally:
            self._log_trim_busy = False

    def _copy_log_selection(self, event=None):
        try:
            text = self.log.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.log.get("1.0", "end-1c")     # nothing selected: copy all
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
        return "break"

    def _select_log_all(self, event=None):
        self.log.tag_add("sel", "1.0", "end-1c")
        self.log.focus_set()
        return "break"

    def say(self, text, tag="dim"):
        """Write one line into Activity.

        The engine stamps its own lines with a time; the window did not, so the
        log read as two different logs interleaved. Stamp ours to match.
        """
        text = text.rstrip()
        if not text.startswith("["):
            text = time.strftime("[%H:%M:%S] ") + text
        self.log.configure(state="normal")
        self.log.insert("end", latin(text) + chr(10), tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        while True:
            try:
                line = self.lines.get_nowait()
            except queue.Empty:
                break
            low = line.lower()
            tag = "dim"
            if "conflict" in low or "failed" in low or "refused" in low:
                tag = "bad"
            elif "pushed" in low or "integrated" in low or "resumed" in low or "published" in low:
                tag = "ok"
            elif "paused" in low or "requested" in low:
                tag = "warn"
            self.say(line, tag)
        self.after(150, self._drain)

    def _tail_log(self):
        """The engine is detached, so its story is read from .teamsync.log."""
        self.after(700, self._tail_log)
        if not self.repo:
            return
        logf = os.path.join(self.repo, ".teamsync.log")
        try:
            size = os.path.getsize(logf)
        except OSError:
            return
        if size < self._log_pos:      # log was truncated or replaced
            self._log_pos = 0
        if size > self._log_pos:
            try:
                with open(logf, "r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(self._log_pos)
                    chunk = fh.read()
                    self._log_pos = fh.tell()
                for line in chunk.splitlines():
                    if line.strip():
                        self.lines.put(line)
            except OSError:
                pass

    # -- daemon ------------------------------------------------------------

    def sync_running(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        return bool(self.repo) and daemon_pid(self.repo) is not None

    def start_sync(self):
        if not self.repo:
            return
        if self.sync_running():
            # An engine from a previous window is still covering this folder -
            # but an engine deliberately outlives windows, which means it can
            # outlive UPDATES and keep running last week's code with none of
            # the new behavior, silently. Field-found: a leftover engine ran
            # 2.0.7 rules under a 2.0.15 app. Same version: adopt it. Any
            # other answer (or none - engines before 2.0.16 did not say):
            # replace it with one running this build's code.
            running = daemon_state(self.repo).get("version", "")
            if running == APP_VERSION:
                self.say("sync engine already running in the background - reconnected", "ok")
                self.btn_sync.config(text="Stop sync")
                return
            self.say("engine was running older code - restarting it on "
                     + latin(APP_VERSION), "warn")
            pid = daemon_pid(self.repo)
            if pid:
                kill_pid(pid)
            try:
                os.remove(os.path.join(self.repo, ".teamsync.lock"))
            except OSError:
                pass
        script = resource_path("engine", "teamsync.ps1")
        if not os.path.exists(script):
            explain('The sync engine could not be found.',
                    'A file that ships inside the program is not where it should be. Usually the program file was copied incompletely, or antivirus quarantined part of it.',
                    'Close the app and open it again. If it keeps happening, get a fresh copy from Amin.')
            return
        # DETACHED on purpose: no pipes to this window, so closing the window can
        # never break or stop the engine. Its output reaches us via the log file.
        try:
            self.proc = subprocess.Popen(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 "-Path", self.repo, "-NoPopup", "-AppVersion", APP_VERSION],
                cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            explain('Syncing could not be started.',
                    f'Windows refused: {exc}',
                    'Close the app and open it again. If it repeats, check that git is installed - run  git --version  in a terminal.')
            return
        self.btn_sync.config(text="Stop sync")
        self.pill.set("syncing", ACCENT)
        self.say("sync engine started", "ok")

    def stop_sync(self, quiet=False):
        stopped = False
        # Tell the other side we are going, before the engine is killed - a killed
        # process cannot announce its own departure.
        if self.repo and self.sync_running():
            threading.Thread(target=clear_my_presence,
                             args=(self.repo, presence_name(self.repo)), daemon=True).start()
        if self.proc is not None and self.proc.poll() is None:
            kill_pid(self.proc.pid)
            stopped = True
        self.proc = None
        if self.repo:
            pid = daemon_pid(self.repo)
            if pid:
                kill_pid(pid)
                stopped = True
            try:
                os.remove(os.path.join(self.repo, ".teamsync.lock"))
            except OSError:
                pass
        self.btn_sync.config(text="Start sync")
        self.pill.set("stopped", MUTED)
        if stopped and not quiet:
            self.say("sync stopped - nothing is sent or received until you start it again", "warn")

    def toggle_sync(self):
        if not self.sync_running():
            self.start_sync()
            return

        # Stopping is a real decision, not a toggle: nothing moves in either
        # direction afterwards, and the other person keeps working against a
        # copy that is quietly falling behind.
        waiting = ""
        if self.repo:
            ahead = git(self.repo, "rev-list", "--count", "origin/main..HEAD") or "0"
            dirty = len(git(self.repo, "status", "--porcelain").splitlines())
            bits = []
            if ahead != "0":
                bits.append(f"{ahead} commit(s) not yet published")
            if dirty:
                bits.append(f"{dirty} file(s) changed but not committed")
            if bits:
                waiting = ("You have " + " and ".join(bits) +
                           ". None of it is lost, but none of it reaches them "
                           "until you start syncing again." + NN)

        if not messagebox.askyesno(
            APP_NAME,
            "Stop syncing this project?" + NN + waiting +
            "While it is off, nothing is sent and nothing is received. Your work "
            "stays safe in local commits, and the background engine is shut down "
            "until you press Start sync." + NN +
            "Closing the window is not the same thing - that leaves syncing running."):
            return
        self.stop_sync()

    # -- actions -----------------------------------------------------------

    def push_now(self):
        """Same request the AI agents make: publish, do not wait for the backstop."""
        if not self.repo:
            return
        if git(self.repo, "diff", "--name-only", "--diff-filter=U"):
            explain('Nothing was published.',
                    'A conflict is open. Nothing goes out until it is finished - and nothing of yours has been lost.',
                    'Press "Open both versions" to compare them, or tell your AI agent: resolve the conflict.', kind='warn')
            return
        if self.sync_running():
            open(os.path.join(self.repo, ".teamsync-push-now"), "w").close()
            self.say("publish requested", "warn")
        else:
            script = os.path.join(self.repo, "push-now.ps1")
            if not os.path.exists(script):
                explain('Nothing could be published.',
                        'Syncing is off and push-now.ps1 is not in the project folder either.',
                        'Press Start sync - the engine publishes on its own.', kind='warn')
                return
            threading.Thread(target=self._run_and_log,
                             args=([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                    "-File", script],), daemon=True).start()

    def _run_and_log(self, cmd, cwd=None):
        if cwd is None:
            cwd = self.repo if (self.repo and os.path.isdir(self.repo)) else None
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace", bufsize=1, creationflags=CREATE_NO_WINDOW)
            for line in proc.stdout:
                if line.strip():
                    self.lines.put(line.rstrip())
            proc.wait()
            return proc.returncode
        except Exception as exc:
            self.lines.put(f"failed: {exc}")
            return 1

    def _show_help(self):
        """One window that makes the app self-sufficient.

        Three tabs: how to get connected, what every part of the app means,
        and the complete standing prompt for an AI agent - so a new team
        needs nothing beyond the program itself to start working, and
        nothing beyond the third tab to bring their agents in.
        """
        win = tk.Toplevel(self)
        win.title("TeamSync help")
        win.geometry("880x560")
        win.configure(bg=BG)
        book = ttk.Notebook(win)
        book.pack(fill="both", expand=True, padx=10, pady=10)

        pages = (("Getting connected", "1-connect.md", MONO),
                 ("How it works", "2-how-it-works.md", MONO),
                 ("AI prompt", "3-agent-prompt.md", MONO))
        for title, fname, font in pages:
            frame = ttk.Frame(book)
            book.add(frame, text=title)
            txt = tk.Text(frame, font=font, relief="flat", wrap="word",
                          padx=14, pady=12, background=PANEL, foreground=FG,
                          insertbackground=FG, selectbackground="#31405e",
                          selectforeground="#eaf0ff",
                          inactiveselectbackground="#31405e",
                          exportselection=False)
            bar = ttk.Scrollbar(frame, command=txt.yview)
            txt.configure(yscrollcommand=bar.set)
            path = resource_path("assets", "help", fname)
            try:
                body = open(path, encoding="utf-8").read()
            except OSError:
                body = "This help page is missing from the installation."
            txt.insert("1.0", body)
            txt.configure(state="disabled")
            txt.bind("<Button-1>", lambda e, t=txt: t.focus_set())

            def copy_sel(event=None, t=txt):
                try:
                    chunk = t.get("sel.first", "sel.last")
                except tk.TclError:
                    chunk = t.get("1.0", "end-1c")
                if chunk:
                    self.clipboard_clear()
                    self.clipboard_append(chunk)
                return "break"

            txt.bind("<Control-c>", copy_sel)
            txt.bind("<Control-a>", lambda e, t=txt: (t.tag_add("sel", "1.0", "end-1c"),
                                                      t.focus_set(), "break")[-1])
            if fname.startswith("3-"):
                row = ttk.Frame(frame)
                ttk.Button(row, text="Copy the whole prompt",
                           command=copy_sel).pack(side="right")
                row.pack(side="bottom", fill="x", pady=(6, 0))
                bar.pack(side="right", fill="y")
                txt.pack(side="left", fill="both", expand=True)
            else:
                bar.pack(side="right", fill="y")
                txt.pack(side="left", fill="both", expand=True)

    def _show_history(self):
        """Older log lines, one button away instead of scrolling forever.

        The engine keeps the live Activity view to roughly today and never
        fewer than the last hundred lines; everything older is appended to a
        per-project history file, which this window shows - read-only but
        fully selectable and copyable, like the live log.
        """
        hist = os.path.join(self.repo, ".teamsync-history.log")
        if not os.path.exists(hist):
            explain('No history yet.',
                    'Nothing has been moved out of the live log so far - '
                    'everything that happened is still on the main screen.',
                    'Older lines start collecting here once the live log '
                    'outgrows a day and a hundred entries.', kind='info')
            return
        win = tk.Toplevel(self)
        win.title("Activity history - " + (os.path.basename(self.repo.rstrip(chr(92) + "/")) or self.repo))
        win.geometry("860x520")
        win.configure(bg=BG)
        txt = tk.Text(win, font=MONO, relief="flat", wrap="word", padx=12, pady=10,
                      background=PANEL, foreground=FG, insertbackground=FG,
                      selectbackground="#31405e", selectforeground="#eaf0ff",
                      inactiveselectbackground="#31405e", exportselection=False)
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        try:
            body = open(hist, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            body = "could not read the history file: " + str(exc)
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        txt.bind("<Button-1>", lambda e: txt.focus_set())

        def copy_sel(event=None):
            try:
                chunk = txt.get("sel.first", "sel.last")
            except tk.TclError:
                chunk = txt.get("1.0", "end-1c")
            if chunk:
                self.clipboard_clear()
                self.clipboard_append(chunk)
            return "break"

        txt.bind("<Control-c>", copy_sel)
        txt.bind("<Control-a>", lambda e: (txt.tag_add("sel", "1.0", "end-1c"),
                                           txt.focus_set(), "break")[-1])
        txt.see("end")

    def _open_folder(self):
        if self.repo:
            os.startfile(self.repo)

    def _open_conflict(self):
        target = self.conflict_dir or os.path.join(self.repo, "_conflicts")
        if os.path.isdir(target):
            os.startfile(target)

    def _relocate(self):
        """Point this project at a folder that has moved, without restarting.

        A moved folder used to mean closing the window, or Switch - and Switch can
        leave a dead path behind. This changes the location in place: the old
        engine is stopped, the new one starts where the files actually are.
        """
        old = self.repo
        p = self._pick_project("Where is the project now?")
        if not p:
            return
        if os.path.normcase(p) == os.path.normcase(old or ""):
            messagebox.showinfo(APP_NAME, "That is the current location - nothing changed.")
            return
        self.stop_sync(quiet=True)
        # The project moved - it did not become a second project. Drop the old
        # entry, or the list keeps a dead row pointing at a folder that is gone.
        if old:
            forget_project(self.cfg, old)
        self.repo = p
        self.cfg["last_project"] = p
        remember_project(self.cfg, p)
        save_config(self.cfg)
        self._show_project()
        self.say(f"project location changed to {p}", "warn")
        self.start_sync()

    def _switch(self):
        # Leaves the engine running: switching views is not a decision to stop
        # syncing. Stopping is always explicit.
        self.repo = ""
        self._show_welcome()

    def _disconnect(self):
        if not self.repo:
            return
        name = os.path.basename(self.repo.rstrip("\\/"))
        if not messagebox.askyesno(
            APP_NAME,
            f"Disconnect \"{name}\"?\n\n"
            "Syncing stops completely - nothing stays running in the background - and the "
            "project is removed from the app's list.\n\n"
            "The files on disk and the GitHub repository are left untouched.",
        ):
            return
        self.stop_sync(quiet=True)
        forget_project(self.cfg, self.repo)
        if self.cfg.get("last_project") == self.repo:
            self.cfg.pop("last_project", None)
        save_config(self.cfg)
        self.say(f"disconnected from {name}", "warn")
        self.repo = ""
        self._show_welcome()

    def _make_shortcut(self):
        ok, detail = create_desktop_shortcut()
        if ok:
            self.cfg["desktop_shortcut"] = detail
            self.cfg["desktop_shortcut_for"] = os.path.abspath(sys.executable)
            self.cfg["shortcut_icon_for"] = APP_VERSION
            save_config(self.cfg)
            self.shortcut_row.pack_forget()   # Help keeps the strip alive
            explain('The shortcut is on your desktop.',
                    'It points at the program where it is installed, so it keeps '
                    'working after an update.',
                    'Do not drag TeamSync.exe itself onto the desktop - separated '
                    'from the folder it sits in, it cannot start.', kind='info')
        else:
            explain('The shortcut could not be created.', detail,
                    'You can make one by hand: right-click TeamSync.exe, then '
                    'Send to, then Desktop (create shortcut).', kind='warn')

    def _toggle_autostart(self):
        want = self.autostart_var.get()
        if want and not getattr(sys, "frozen", False):
            self.autostart_var.set(False)
            explain('That option does not work here.',
                    'Start with Windows is only possible from the built TeamSync.exe, not when running from source.',
                    'Run TeamSync.exe if you want this.', kind='info')
            return
        if not set_autostart(want):
            self.autostart_var.set(autostart_enabled())

    def _pick_project(self, title):
        """Ask for a project folder and validate it. None if the user backed out.

        Picking the folder ABOVE the project is the usual slip, especially right
        after moving it somewhere new, so look one level down and offer what is
        actually there instead of just refusing.
        """
        p = filedialog.askdirectory(title=title)
        if not p:
            return
        p = os.path.normpath(p)
        if not os.path.isdir(os.path.join(p, ".git")):
            # Picking the folder ABOVE the project is the usual slip, especially
            # right after moving it somewhere new. Look one level down and offer
            # what is actually there instead of just refusing.
            inside = []
            try:
                for name in os.listdir(p):
                    child = os.path.join(p, name)
                    if os.path.isdir(os.path.join(child, ".git")):
                        inside.append(child)
            except OSError:
                pass
            if len(inside) == 1:
                if messagebox.askyesno(
                    APP_NAME,
                    f"This folder is not a project itself, but there is one inside it:\n\n"
                    f"{os.path.basename(inside[0])}\n\n"
                    "Open that one?"):
                    p = inside[0]
                else:
                    return
            elif len(inside) > 1:
                names = "\n".join("  - " + os.path.basename(c) for c in inside)
                messagebox.showinfo(
                    APP_NAME,
                    "This folder is not a project itself. The projects are one level down:\n\n"
                    f"{names}\n\n"
                    "Pick one of those, not this folder.")
                return
            else:
                messagebox.showwarning(
                    APP_NAME,
                    "This folder is not a shared project - there is no .git inside it.\n\n"
                    f"{p}\n\n"
                    "Pick the project folder itself, not the folder above it.")
                return
        return p

    def _locate_missing(self):
        """The welcome screen's shortcut for a project whose folder moved."""
        gone = self.cfg.get("last_project")
        p = self._pick_project("Where is the project now?")
        if not p:
            return
        if gone and os.path.normcase(gone) != os.path.normcase(p):
            forget_project(self.cfg, gone)
        remember_project(self.cfg, p)
        self.repo = p
        self.cfg["last_project"] = p
        save_config(self.cfg)
        self._show_project()
        self.start_sync()

    def _open_existing(self):
        """Show what has been opened before. Browsing is the fallback, not the ritual."""
        dlg = ProjectChooser(self, self.cfg.get("projects", []))
        self.wait_window(dlg)
        if not dlg.result:
            return
        action, path = dlg.result

        if action == "forget":
            forget_project(self.cfg, path)
            save_config(self.cfg)
            self.say(f"removed {path} from the list - nothing on disk was touched", "warn")
            return self._open_existing()

        if action == "open" and not os.path.isdir(os.path.join(path, ".git")):
            # Listed but gone. Offer to point at where it lives now rather than
            # making the person start over.
            if not messagebox.askyesno(
                APP_NAME,
                "That folder is not there any more:" + NN + path + NN +
                "If you moved it, show me where it is now?"):
                return
            action = "browse"
            forget_project(self.cfg, path)

        if action == "browse":
            path = self._pick_project("Open a project folder")
            if not path:
                return
        p = path
        self.repo = os.path.normpath(p)
        self.cfg["last_project"] = self.repo
        save_config(self.cfg)
        self._show_project()
        self.start_sync()

    # -- setup -------------------------------------------------------------

    def _setup(self, mode):
        dlg = SetupDialog(self, mode, self.cfg)
        self.wait_window(dlg)
        if not dlg.result:
            return
        v = dlg.result
        for key in ("me", "email", "friend", "owner"):
            if v.get(key):
                self.cfg[key] = v[key]

        script = resource_path("engine", "init-owner.ps1" if mode == "owner" else "init-friend.ps1")
        cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-NoWatch"]
        if mode == "owner":
            cmd += ["-Path", v["path"]]
            if v.get("reponame"):
                cmd += ["-RepoName", v["reponame"]]
            if v.get("friend"):
                cmd += ["-Friend", v["friend"]]
        else:
            cmd += ["-RepoName", v["reponame"], "-Path", v["path"]]
            if v.get("owner"):
                cmd += ["-Owner", v["owner"]]
        if v.get("me"):
            cmd += ["-Me", v["me"]]
        if v.get("email"):
            cmd += ["-MyEmail", v["email"]]

        self.repo = os.path.normpath(v["path"])
        self._show_project()
        self.say("setting up...", "warn")
        self.pill.set("setting up", WARN)
        threading.Thread(target=self._finish_setup, args=(cmd,), daemon=True).start()

    def _finish_setup(self, cmd):
        code = self._run_and_log(cmd)
        if code == 0 and os.path.isdir(os.path.join(self.repo, ".git")):
            self.cfg["last_project"] = self.repo
            save_config(self.cfg)
            self.lines.put("setup finished - starting sync")
            self.after(200, self.start_sync)
        else:
            self.lines.put("setup did not finish. Read the messages above.")

    # -- status ------------------------------------------------------------

    def _poll_git(self):
        self.after(4000, self._poll_git)
        if self.repo and not os.path.isdir(self.repo):
            # Moved or deleted while open. Fall back to the welcome screen and
            # explain, rather than showing a project that is not there.
            gone = self.repo
            self.repo = ""
            self.say(f"the project folder is no longer at {gone}", "bad")
            self._show_welcome()
            self.missing_lbl.config(
                text=f"The project folder is no longer at this path:\n{gone}\nUse \"Open a project already on this machine\" to point at its new location.")
            self.missing.pack(fill="x", pady=(0, 18), before=self.welcome_actions)
            return
        if not self.repo or not os.path.isdir(os.path.join(self.repo, ".git")):
            return
        unmerged = git(self.repo, "diff", "--name-only", "--diff-filter=U")
        if unmerged:
            n = len(unmerged.splitlines())
            self.banner.pack(fill="x", padx=18, pady=(0, 10), before=self.log.master)
            self.banner_lbl.config(
                text=f"Conflict in {n} file(s). Nothing was published, nothing was lost.\n"
                     f"Both versions are saved side by side for you.")
            self.pill.set("conflict", BAD)
            latest = None
            croot = os.path.join(self.repo, "_conflicts")
            if os.path.isdir(croot):
                subs = sorted(d for d in os.listdir(croot) if os.path.isdir(os.path.join(croot, d)))
                if subs:
                    latest = os.path.join(croot, subs[-1])
            self.conflict_dir = latest
            return

        self._refresh_partner()

        self.banner.pack_forget()
        if not self.sync_running():
            self.pill.set("stopped", MUTED)
            self.btn_sync.config(text="Start sync")
            return
        self.btn_sync.config(text="Stop sync")
        ahead = git(self.repo, "rev-list", "--count", "origin/main..HEAD") or "0"
        if daemon_state(self.repo).get("net") == "offline":
            # Offline is not a failure state: work is committed locally and the
            # engine keeps retrying. Say that, rather than just showing a number.
            waiting = f" - {ahead} waiting" if ahead != "0" else ""
            self.pill.set(f"offline, retrying{waiting}", WARN)
        elif ahead != "0":
            self.pill.set(f"{ahead} not published yet", WARN)
        else:
            self.pill.set("everything published", OK)

    def _refresh_partner(self):
        """Green = beating now, amber = seen earlier, grey = never joined."""
        name, ago = partner_presence(self.repo, presence_name(self.repo))
        if name is None:
            label = self.cfg.get("friend") or self.cfg.get("owner") or "partner"
            self.partner_pill.set(f"{label}: not joined yet", MUTED)
            return
        if ago <= 150:          # beat is every 60s; allow two misses before doubting
            self.partner_pill.set(f"{name}: online", OK)
        else:
            self.partner_pill.set(f"{name}: seen {humanise(ago)} ago", WARN)

        busy = partner_pending_files(self.repo, presence_name(self.repo))
        if busy:
            shown = ", ".join(busy[:3]) + (" +%d" % (len(busy) - 3) if len(busy) > 3 else "")
            self.partner_files_lbl.config(text=latin(("%s is working on: " % name) + shown))
            self.partner_files_lbl.pack(anchor="e", after=self.partner_pill.master)
        else:
            self.partner_files_lbl.pack_forget()

    def _check_home(self):
        """Where the exe happens to sit should not decide whether it works."""
        if not getattr(sys, "frozen", False) or exe_home_writable():
            return
        if self.cfg.get("declined_move"):
            return
        here = os.path.dirname(sys.executable)
        if messagebox.askyesno(
            APP_NAME,
            'TeamSync is in a folder it is not allowed to write to:' + NN + here + NN +
            'Because of that it cannot update itself.' + NN +
            'Put a copy of itself in your own programs folder and carry on from there?' + NN +
            preferred_home()):
            moved = relocate_self()
            if moved:
                self.destroy()
                return
            explain('TeamSync could not move itself.',
                    'Copying to ' + preferred_home() + ' did not work.',
                    'Move TeamSync.exe to a folder such as Desktop yourself and run it from there.',
                    kind='warn')
        else:
            self.cfg["declined_move"] = True
            save_config(self.cfg)

    def _update_tick(self):
        """Look for a newer build. Never download one on our own.

        An update that installs itself means the machine publishing it can run
        code here without anyone agreeing to it. The check below only notices;
        the person decides.
        """
        self.after(UPDATE_EVERY_SECONDS * 1000, self._update_tick)
        if self._update_busy or not getattr(sys, "frozen", False):
            return          # source runs have nothing to replace
        if self._update_ready:
            # Already downloaded and verified, just waiting for a safe moment.
            # Returning here instead would leave it waiting for ever.
            self._try_apply_update()
            return
        # An offer already on screen is NOT a reason to stop looking: releases
        # keep coming, and a standing button must follow them - it once sat on
        # 2.0.17 while 2.0.18 was published, and only a window restart moved
        # it. The check costs a 304, which costs nothing.
        self._update_busy = True
        threading.Thread(target=self._look_for_update, daemon=True).start()

    def _look_for_update(self):
        try:
            info = latest_release()
            if not info or not info.get("tag"):
                return
            if _version_tuple(info["tag"]) <= _version_tuple(APP_VERSION):
                return
            offered = self._update_available
            if offered and offered.get("tag") == info["tag"]:
                return                    # same offer already on screen
            if offered:
                self.lines.put("the update offer moved to " + info["tag"])
            self._update_available = info
            self.after(0, self._show_update_offer)
        finally:
            self.show_progress(-1, 0)
            self._update_busy = False
            if self._update_available and not self._update_ready:
                self.after(0, self._show_update_offer)   # put the offer back

    def _show_update_offer(self):
        """A button that stays put until the update is taken or refused."""
        info = self._update_available
        if not info:
            return
        self.update_btn.config(text=latin("Version " + info["tag"] + " is out - get it"))
        self.update_btn.pack(side="right", padx=(8, 0))
        self.lines.put("a newer version is available: " + info["tag"])

    def show_progress(self, done, total, what="downloading"):
        """Called from the download thread; hops back to the UI thread to draw."""
        self.after(0, lambda: self._draw_progress(done, total, what))

    def _draw_progress(self, done, total, what="downloading"):
        if done < 0:                      # a negative total means "finished, hide"
            self.progress.pack_forget()
            return
        if not self.progress.winfo_ismapped():
            self.progress.pack(fill="x", before=self.body)
        width = max(self.progress_bar.winfo_width(), 1)
        frac = (done / total) if total else 0
        self.progress_bar.coords(self._progress_fill, 0, 0, width * min(frac, 1.0), 6)
        if total:
            self.progress_lbl.config(
                text=latin(f"{what}  {done / 1048576:.1f} / {total / 1048576:.1f} MB"
                     f"   ({frac * 100:.0f}%)"))
        else:
            self.progress_lbl.config(text=latin(f"{what}  {done / 1048576:.1f} MB"))

    def _take_update(self):
        if getattr(sys, "frozen", False) and not install_dir_updatable():
            explain('Updates cannot install themselves here.',
                    'TeamSync is running from a folder Windows protects (such as '
                    'Program Files). Replacing the program there needs administrator '
                    'rights the updater does not have.',
                    'Close TeamSync, move the whole TeamSync folder to a normal '
                    'place - for example C:' + chr(92) + 'Apps' + chr(92) + 'TeamSync - '
                    'open it from there, and press the update button again.',
                    kind='warn')
            return

        info = self._update_available
        if not info or self._update_busy:
            return
        mb = info.get("size", 0) / (1024 * 1024)
        size_txt = ("%.1f MB" % mb) if mb else "unknown"
        if not messagebox.askyesno(
            APP_NAME,
            "A newer version has been released:" + NN +
            "  version : " + info["tag"] + N +
            "  size    : " + size_txt + N +
            "  date    : " + (info.get("date") or "-") + N +
            "  you are on : v" + APP_VERSION + NN +
            "Download and install it?" + NN +
            "Before anything is run, its signature is checked and the file is handed to your antivirus. If either objects, it is not installed."):
            return
        self._update_busy = True
        self.update_btn.config(text="downloading...")
        threading.Thread(target=self._fetch_update, daemon=True).start()
    def _fetch_update(self):
        """Download the offered build, check it, then hand the decision back.

        Runs on a worker thread, so it never opens a dialog itself: Tk windows
        belong to the main thread, and asking from here is how the button ended
        up stuck at 'downloading' with nothing behind it.
        """
        target = None
        try:
            info = self._update_available or {}
            tag = info.get('tag')
            if not tag:
                return
            target = os.path.join(staging_dir(), PACKAGE_NAME)
            size = info.get('size', 0)

            self.show_progress(0, size, 'downloading ' + tag)
            ok = download_asset(info.get('exe_id'), target, size,
                                lambda d, t: self.show_progress(d, t, 'downloading ' + tag))
            if not ok:
                # The streaming path needs an API token; gh can do it without one.
                self.show_progress(0, 0, 'downloading ' + tag)
                ok = download_release(tag, target, PACKAGE_NAME)
            if not ok:
                self.lines.put('update download failed - the offer is still there')
                self.after(0, lambda: explain(
                    'The new version could not be downloaded.',
                    'Either the network or VPN dropped, or the update repository is not reachable.',
                    'Check the VPN, then press the button again. Nothing was changed.', kind='warn'))
                return

            sig = target + '.sig'
            got_sig = download_asset(info.get('sig_id'), sig, 0) or download_signature(tag, sig)
            ok, why = verify_release(target, sig) if got_sig else (False, 'no signature was published')
            if not ok:
                self._discard(target)
                self.lines.put('REFUSED update ' + tag + ': ' + why)
                self.after(0, lambda: explain(
                    'A new version arrived but was not installed.',
                    'Its signature did not match the key built into this app: ' + why,
                    'This is deliberate and protects you: a build that cannot be vouched for is never run. Tell Amin you saw this. The app carries on unchanged.'))
                return
            self.lines.put('update ' + tag + ' signature verified')

            self.lines.put('handing the file to the antivirus before running it')
            verdict, detail = scan_with_antivirus(target)
            self.lines.put('antivirus: ' + verdict + ' - ' + detail)
            if verdict == 'infected':
                self._discard(target)
                self.after(0, lambda: explain(
                    'The new version was not installed.',
                    'Your antivirus objected to the file: ' + detail,
                    'It has been deleted and the app carries on unchanged. Tell Amin - something is wrong on the publishing side.'))
                return

            self.lines.put("unpacking " + tag)
            unpacked = unpack_update(target)
            if not unpacked:
                self._discard(target)
                self.after(0, lambda: explain(
                    'The new version could not be unpacked.',
                    'The downloaded package was not readable, or the disk refused it.',
                    'Nothing was changed and the app carries on. Press the button again '
                    'later, or ask Amin for the file.', kind='warn'))
                return

            # Decisions and dialogs belong to the main thread.
            self.after(0, lambda: self._update_downloaded(unpacked, verdict, detail))
        except Exception as exc:
            self.lines.put('update failed: ' + str(exc))
            if target:
                self._discard(target)
        finally:
            self.show_progress(-1, 0)
            self._update_busy = False
            if self._update_available and not self._update_ready:
                self.after(0, self._show_update_offer)   # put the offer back

    def _discard(self, path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _update_downloaded(self, target, verdict, detail):
        """Main thread: ask anything that needs asking, then install."""
        if verdict == 'unknown':
            # Two different situations, and lumping them together helps nobody:
            # no scanner installed at all, versus a scanner that answered vaguely.
            none_found = 'no antivirus' in detail.lower()
            if none_found:
                question = ('No antivirus was found on this machine, so the file could '
                            'not be checked for malware.' + NN +
                            'Its signature IS valid, which proves it came from the release '
                            'key and has not been altered.' + NN +
                            'Install it without a virus check?')
            else:
                question = ('The signature is valid, but your antivirus gave no clear '
                            'answer:' + NN + detail + NN + 'Install it anyway?')
            if not messagebox.askyesno(APP_NAME, question + NN +
                                       'If you say no, the update button stays where it is '
                                       'and you can decide later.'):
                self._discard(target)
                self.lines.put('update declined - not scanned' if none_found
                               else 'update declined after an inconclusive scan')
                self._show_update_offer()      # the offer stays, as promised
                return
        self._update_ready = target
        self._try_apply_update()

    def _try_apply_update(self):
        """Restart into the new build, but never in the middle of something."""
        if not self._update_ready:
            return
        if self.repo and git(self.repo, "diff", "--name-only", "--diff-filter=U"):
            return          # a conflict is open; leave the user alone
        if self.repo and (git(self.repo, "rev-list", "--count", "origin/main..HEAD") or "0") != "0":
            return          # unpublished work in flight; wait for the next tick
        # The engine is bundled in the exe too, so it has to come up again on the
        # new code. Work is already in local commits, so stopping is safe.
        repo = self.repo
        # Say it before doing it. The window vanishing and coming back with no
        # warning looks exactly like a crash to someone who was not told.
        self.say("updating - this window will close and reopen in a moment", "warn")
        self.pill.set("updating...", ACCENT)
        self.update_idletasks()
        self._update_available = None
        self.update_btn.pack_forget()
        self.cfg["updated_from"] = APP_VERSION
        if repo:
            self.cfg["last_project"] = repo
        save_config(self.cfg)
        self.after(1800, lambda: self._finish_update(repo))

    def _finish_update(self, repo):
        # The engine runs out of the folder we are about to replace, so it has to
        # let go first. The new copy starts it again.
        self.stop_sync(quiet=True)
        if apply_update(self._update_ready):
            self.destroy()
            os._exit(0)          # the helper waits for this pid; die now, not at GC's leisure
        else:
            self.lines.put("could not swap the program file - will retry later")
            explain('The update did not finish.',
                    'The program file could not be swapped for the new one. Usually that means antivirus is holding the file, '
                    'or TeamSync is running from a folder it cannot write to.',
                    'It carries on with the current version and nothing is broken. Move TeamSync.exe to a folder such as '
                    'Desktop and run it from there; the next update will apply itself.', kind='warn')
            self._update_ready = None
            self.start_sync()

    def _close(self):
        if self.repo:
            self.cfg["last_project"] = self.repo
            save_config(self.cfg)
        # Closing the window is not a decision to stop syncing. The engine is
        # detached and keeps running; Stop sync / Disconnect are the off switches.
        if self.sync_running() and not self.cfg.get("bg_notice_shown"):
            messagebox.showinfo(
                APP_NAME,
                "The window is closing, but syncing carries on in the background.\n\n"
                "To really stop it, use \"Stop sync\" or \"Disconnect\" next time."
                + NN + "(Shown once.)",
            )
            self.cfg["bg_notice_shown"] = True
            save_config(self.cfg)
        self.destroy()


def leave_install_folder():
    """Move this process's working directory out of the folder we run from.

    A process standing in a folder locks it against renaming, and every child
    spawned without an explicit cwd inherits the parent's. The app is launched
    by double-click or by its shortcut, both of which stand it in the install
    folder - so the app itself, and any helper or gh call it spawns, would
    quietly hold the one folder an update must replace. Leaving once at
    startup ends the whole class.
    """
    try:
        os.chdir(os.environ.get("TEMP") or os.path.expanduser("~"))
    except OSError:
        pass


def main():
    leave_install_folder()
    # A windowed build has no console, so an unhandled error would otherwise kill
    # the app silently and look like "it did nothing". Say what happened, and
    # leave a file behind that can be read afterwards.
    # During an update the outgoing copy starts the new one and then quits, so a
    # brief overlap is normal. Wait it out before deciding a second copy is real.
    if not claim_instance_lock():
        time.sleep(2.0)
        if not claim_instance_lock():
            # Do NOT open a dialog here. A modal box waits for a click that may
            # never come, and the second copy then sits in memory holding the
            # program file open - which is exactly what stopped updates from
            # applying. Surface the window that is already there and step aside.
            raise_existing_window()
            return

    try:
        app = App()
        if "--autostart" in sys.argv:
            app.iconify()
        app.mainloop()
    except Exception:
        import traceback
        report = traceback.format_exc()
        try:
            path = os.path.join(os.path.dirname(config_path()), "crash.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(report)
        except Exception:
            path = "(could not be written)"
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, f"{APP_NAME} could not start.\n\n"
                                           f"{report.strip().splitlines()[-1]}\n\n"
                                           f"Full details: {path}")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
