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
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import filedialog, messagebox, ttk

try:
    import sv_ttk
except Exception:                     # packaging or antivirus can lose it
    sv_ttk = None

APP_NAME = "TeamSync"
# Version rule, set 2026-08-29: only the FIRST part may pass 9. The second
# and third are single digits, so the line runs 2.1.0 ... 2.1.9, then 2.2.0,
# on to 2.9.9, and then 3.0.0. publish-release.ps1 refuses anything else, so
# the rule cannot be broken by forgetting it.
APP_VERSION = "2.2.2"           # compared against the newest release tag
UPDATE_REPO = "aminbm1919/teamsync-app"   # public since 2026-08-28: source and releases
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
# The startup sweep that moves every running engine onto this build (see
# App._sweep_engines). Only the built app runs it: a source run is a test or a
# developer, and must not reach into the engines of the installed app.
SWEEP_ENGINES_AT_START = bool(getattr(sys, "frozen", False))
REPLACE_WAIT = 120      # seconds an engine gets to come to its rest between passes
REPLACE_QUIET = 0.3     # no command seen for this long = it is resting, not between two
REPLACE_POLL = 0.05     # how often to look
ENGINE_UP_WAIT = 60     # seconds for a restarted engine's first heartbeat
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
    """Run one git command in repo. Returns stdout stripped, or '' on failure.

    Decoded as UTF-8 explicitly, not through the machine's locale. Python's
    text mode would use the console codepage - cp1252 here - and git speaks
    UTF-8, so every non-latin byte came back as mojibake: a file reading
    "سطر از امین" arrived as "Ø³Ø·Ø± Ø§Ø² Ø§Ù…ÛŒÙ†". Harmless while
    this helper was only counting commits and reading ref names, which are
    ascii; wrong the moment it is asked for a file's CONTENT.
    """
    try:
        out = subprocess.run(
            ["git"] + list(args), cwd=repo, capture_output=True,
            creationflags=CREATE_NO_WINDOW, env=_GIT_ENV,
        )
        if out.returncode != 0:
            return ""
        return out.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""


# The window only ever READS a project while the engine writes it, so its git
# calls must not take git's optional index lock: a reader holding it for a
# moment made the engine's `git add` fail. Locks git truly needs (a checkout,
# a config write) are not optional and are still taken.
_GIT_ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0")


def git_bytes(repo, *args):
    """Run one git command in repo. Returns stdout as bytes, or None on failure.

    For output that is NUL-separated (-z) or has to be exact: nothing stripped,
    nothing guessed, and a failure is None - never an empty answer that could
    be read as "nothing there".
    """
    try:
        out = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True,
                             creationflags=CREATE_NO_WINDOW, env=_GIT_ENV)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


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


def process_start(pid):
    """When a process began, as a Windows FILETIME, or None.

    Windows hands process ids out again after a process ends, so an id alone
    cannot prove a particular program is the one holding it. The birth moment
    can: a recycled id belongs to a process that started later.
    """
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not handle:
        return None
    try:
        created = ctypes.c_ulonglong()
        rest = [ctypes.c_ulonglong() for _ in range(3)]
        ok = k32.GetProcessTimes(handle, ctypes.byref(created),
                                 *[ctypes.byref(x) for x in rest])
        return created.value if ok else None
    finally:
        k32.CloseHandle(handle)


def daemon_pid(repo):
    """PID of a live engine for this repo, or None.

    Decided on the PROCESS, not on how recently it managed to write. The old
    rule refused to believe in an engine whose lock was more than 30 seconds
    old - but the engine writes that lock once per pass, and a pass with many
    network round trips takes longer than that. A healthy engine then looked
    dead, and the window offered to start a second one on the same folder;
    two engines committing and rebasing one worktree is the single thing git
    cannot survive.

    The engine records its start time in the lock, so a recycled id is not
    mistaken for it. A lock written before that scheme keeps the old rule.
    """
    lock = os.path.join(repo, ".teamsync.lock")
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1 writes a byte-order mark
        # at the head of the file, which makes the first line "﻿pid=..." and
        # not "pid=...". Read as plain utf-8 this returned None for a perfectly
        # healthy engine, so a window could never reconnect to one left running
        # in the background - the whole point of the engine being detached.
        fields = {}
        with open(lock, "r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    fields.setdefault(k, v)
        pid = int(fields.get("pid", ""))
        if not pid_alive(pid):
            return None
        recorded = fields.get("started", "")
        if recorded:
            # The engine writes .NET's round-trip form; compare through a
            # parsed value so the two clocks are the same clock.
            import datetime as _dt
            try:
                want = _dt.datetime.fromisoformat(recorded)
            except ValueError:
                return pid
            actual = process_start(pid)
            if actual is None:
                return None
            # FILETIME: 100 ns ticks since 1601-01-01, in UTC.
            born = (_dt.datetime(1601, 1, 1, tzinfo=_dt.timezone.utc)
                    + _dt.timedelta(microseconds=actual / 10))
            if want.tzinfo is None:
                want = want.astimezone()
            return pid if abs((born - want).total_seconds()) < 2 else None
        import time
        if time.time() - os.path.getmtime(lock) > 30:
            return None
        return pid
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


# ------------------------------------------------------------ the engines ---


def engine_script():
    """The engine this copy of the app runs."""
    return resource_path("engine", "teamsync.ps1")


_FINGERPRINTS = {}


def engine_fingerprint(script=None):
    """Which build of the engine this is - its CODE, not its version label.

    Two builds can carry one version number, and a label cannot tell them
    apart. Found 2026-10-03: a project's engine came from the first build of
    2.2.1 and ran that build's code under the second for a day and a half,
    because both said "2.2.1". The engine hashes the same files when it starts
    (Get-EngineBuild in sync-core.ps1) and writes the result in its heartbeat:
    the first 12 hex digits of SHA-256 over the engine script and then the
    sync-core.ps1 beside it, the bytes as they are on disk. '' if unreadable.
    """
    script = script or engine_script()
    core = os.path.join(os.path.dirname(script), "sync-core.ps1")
    try:
        stamp = (script, os.path.getmtime(script),
                 os.path.getmtime(core) if os.path.isfile(core) else 0)
    except OSError:
        return ""
    if stamp not in _FINGERPRINTS:
        h = hashlib.sha256()
        try:
            for path in (script, core):
                if path == script or os.path.isfile(path):
                    with open(path, "rb") as fh:
                        h.update(fh.read())
        except OSError:
            return ""
        _FINGERPRINTS[stamp] = h.hexdigest()[:12]
    return _FINGERPRINTS[stamp]


def engine_command(repo):
    """The one command every engine is started with: this copy's engine,
    told which build it is - the version its heartbeat then carries."""
    return [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", engine_script(),
            "-Path", repo, "-NoPopup", "-AppVersion", APP_VERSION]


def launch_engine(repo):
    """Start repo's engine DETACHED: no pipes to any window, so closing a
    window can never break or stop it - its story reaches the window through
    the log file. Returns the Popen; raises if Windows refuses.

    Started from a neutral folder, never from the project. A process holds the
    folder it was started in against being moved or renamed, and the engine's
    own Set-Location does not release it: PowerShell moves its location, not
    the process's. Measured 2026-10-02: started with cwd=<project>, as the
    window always started it, the engine held the project folder for its
    whole life ("being used by another process"); started elsewhere, the
    folder moved freely - with the REAL engine and its file watcher too, which
    then stopped by itself within two seconds. Same family as the 2.0.7 update
    helper that held the very folder it had to replace.

    A stop request left from an engine that is gone would stop this one at
    its first pass: it is cleared first (the engine clears it too).
    """
    clear_stop_request(repo)
    return subprocess.Popen(engine_command(repo), cwd=tempfile.gettempdir(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)


# The window knows an engine by the heartbeat it writes into its project:
# pid, start time, version. Finding engines the window was never told about,
# and stopping one at a safe moment, means looking at the processes
# themselves. These calls ask Windows directly - not WMI, which is slow, is
# sometimes broken, and would hand a Persian path back through a code page
# (Set-GitOutputUtf8 in sync-core.ps1 has what that does).

class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


_WINAPI = {}


def _winapi():
    """kernel32, ntdll and shell32 with their prototypes declared.

    Undeclared, ctypes passes and returns plain ints, which cuts a 64-bit
    handle in half. Declared on library objects of their own, so the calls
    the rest of the app makes (pid_alive, process_start) are untouched.
    """
    if not _WINAPI:
        handle = ctypes.c_void_p
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = handle
        k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        for fn in (k32.Process32FirstW, k32.Process32NextW):
            fn.restype = ctypes.c_int
            fn.argtypes = [handle, ctypes.POINTER(_ProcessEntry)]
        k32.OpenProcess.restype = handle
        k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k32.CloseHandle.argtypes = [handle]
        k32.TerminateProcess.restype = ctypes.c_int
        k32.TerminateProcess.argtypes = [handle, ctypes.c_uint]
        k32.WaitForSingleObject.restype = ctypes.c_ulong
        k32.WaitForSingleObject.argtypes = [handle, ctypes.c_ulong]
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.restype = ctypes.c_ulong
        nt.NtQueryInformationProcess.argtypes = [handle, ctypes.c_ulong, ctypes.c_void_p,
                                                 ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        sh = ctypes.WinDLL("shell32")
        sh.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
        sh.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
        _WINAPI.update(k32=k32, nt=nt, sh=sh)
    return _WINAPI


def process_table():
    """[(pid, parent pid, program)] for every process now running; [] if
    Windows will not say."""
    k32 = _winapi()["k32"]
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)          # TH32CS_SNAPPROCESS
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        table = []
        more = k32.Process32FirstW(snap, ctypes.byref(entry))
        while more:
            table.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            more = k32.Process32NextW(snap, ctypes.byref(entry))
        return table
    finally:
        k32.CloseHandle(snap)


def process_command_line(pid):
    """The command line a process was started with, or None."""
    w = _winapi()
    handle = w["k32"].OpenProcess(0x1000, 0, int(pid))   # QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size, need = 2048, ctypes.c_ulong(0)
        for _ in range(4):
            buf = ctypes.create_string_buffer(size)
            status = w["nt"].NtQueryInformationProcess(handle, 60, buf, size,  # command line
                                                       ctypes.byref(need))
            if status in (0xC0000004, 0xC0000023, 0x80000005):  # too small: grow, ask again
                size = max(need.value, size * 2)
                continue
            if status != 0:
                return None
            text = _UnicodeString.from_buffer(buf)
            return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
        return None
    finally:
        w["k32"].CloseHandle(handle)


def split_command_line(line):
    """A command line split exactly as Windows splits it for the program."""
    w = _winapi()
    count = ctypes.c_int(0)
    argv = w["sh"].CommandLineToArgvW(line, ctypes.byref(count))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(count.value)]
    finally:
        w["k32"].LocalFree(ctypes.cast(argv, ctypes.c_void_p))


def _same_folder(a, b):
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def engine_folder(pid):
    """The folder an engine process was started for (its -Path), or None."""
    line = process_command_line(pid)
    if not line:
        return None
    args = split_command_line(line)
    low = [a.lower() for a in args]
    try:
        return args[low.index("-path") + 1]
    except (ValueError, IndexError):
        return None


def engine_serves(pid, repo):
    """Does this engine work on THIS folder? True, False, or None when its
    command line says nothing either way.

    A heartbeat travels with its folder. Moved while its engine runs - which
    a folder can be now - the folder carries a heartbeat that vouches for an
    engine still working on the OLD path, for the second or two before that
    engine notices and stops. Opening the folder at its new place must not
    "reconnect" to an engine about to vanish, and a stop request written
    here would never reach it: its requests are read where it works."""
    folder = engine_folder(pid)
    if folder is None:
        return None
    return _same_folder(folder, repo)


# The engine's stop request (sync-core.ps1, Receive-StopRequest): a file in
# the project's .git, holding "stop" or "restart". The engine takes it only by
# deleting it, and the app withdraws it the same way, so whichever delete
# succeeds decides.
STOP_REQUEST = "teamsync-stop"


def _stop_request_path(repo):
    return os.path.join(repo, ".git", STOP_REQUEST)


def ask_engine_to_stop(repo, mode):
    """Leave repo's engine a request to stop at its next rest. True if written."""
    try:
        with open(_stop_request_path(repo), "w", encoding="ascii") as fh:
            fh.write(mode)
        return True
    except OSError:
        return False


def withdraw_stop_request(repo):
    """Take a request back. True: it was still there, and the engine will now
    never act on it. False: nothing to take back - the engine has already
    taken it, and is on its way out (or there was none)."""
    for _ in range(10):
        try:
            os.remove(_stop_request_path(repo))
            return True
        except FileNotFoundError:
            return False
        except OSError:
            time.sleep(0.05)        # the engine is reading it this instant
    return False


def clear_stop_request(repo):
    try:
        os.remove(_stop_request_path(repo))
    except OSError:
        pass


def stop_requested(repo):
    """Is the person's stop waiting for repo's engine - asked, and not taken
    yet? A replacement's "restart" is not the person's word."""
    try:
        with open(_stop_request_path(repo), encoding="ascii", errors="replace") as fh:
            return fh.read().strip() == "stop"
    except OSError:
        return False


def engines_on_machine(script):
    """{project folder: pid} for every engine on this machine running `script`.

    The app knows the projects it LISTS; an engine knows nothing of the list.
    Measured 2026-10-02: the engine still running 2.1.9 code - the marker refs
    that carried unsent commits to GitHub - served a test project that had
    dropped off the list while its engine ran on. Walking the list alone would
    never have reached the one engine this exists for.

    Only engines of THIS copy's script. An update swaps the program folder in
    place, so every engine this installation ever started runs from the same
    path; another copy of TeamSync - a test, a source run - answers for its own.
    """
    want = os.path.normcase(os.path.abspath(script))
    name = os.path.basename(want)
    found = {}
    for pid, _parent, program in process_table():
        if program.lower() not in ("powershell.exe", "pwsh.exe"):
            continue
        line = process_command_line(pid)
        if not line or name not in line.lower():
            continue
        args = split_command_line(line)
        low = [a.lower() for a in args]
        try:
            ran = args[low.index("-file") + 1]
            folder = args[low.index("-path") + 1]
        except (ValueError, IndexError):
            continue
        if os.path.normcase(os.path.abspath(ran)) == want:
            found[folder] = pid
    return found


def engine_busy(pid, born):
    """Is the engine in the middle of a command - git, gh - right now?

    Its child processes say so. The engine itself never touches the
    repository; git does, in a process of its own, and git is what must not
    be cut off. Killed, it can leave its lock files behind (index.lock, a
    ref's .lock); orphaned, it dies at its next write into the engine's pipe,
    a rebase half done. Either way every later engine then fails on that
    repository with nobody watching. Measured 2026-10-02 on the two live
    engines here: a git child was running in 89-96% of 1,086 samples -
    network pushes and fetches of 6-8 s each - so stopping an engine at a
    random moment catches one nearly every time. Its console host is not
    work. `born` is the engine's own start: a process that names it as parent
    but started earlier belonged to an older holder of the same id.
    """
    for child, parent, program in process_table():
        if parent != pid or program.lower() == "conhost.exe":
            continue
        start = process_start(child)
        if start is not None and born is not None and start < born:
            continue
        return True
    return False


def _engine_pid(repo, tries=3):
    """daemon_pid, asked again before believing "no engine". The engine
    rewrites its heartbeat every pass - truncate, then write - and a read
    landing between the two finds an empty file. Once, that is noise; here it
    would skip a stale engine silently until the next start."""
    for attempt in range(tries):
        pid = daemon_pid(repo)
        if pid or attempt == tries - 1:
            return pid
        time.sleep(0.05)


def stale_engines(listed):
    """Every project folder whose LIVE engine runs code other than this build's.

    Looked for in two places: the projects the app lists, and every engine on
    the machine running this copy's engine script (an engine outlives the list
    - see engines_on_machine). A folder with no engine running is never in the
    answer: a replacement stops one engine and starts one; it never starts one
    where none ran.
    """
    folders = {}
    for path in listed:
        if path:
            folders.setdefault(_engine_key(path), path)
    try:
        for path in engines_on_machine(engine_script()):
            folders.setdefault(_engine_key(path), path)
    except Exception:
        pass            # the processes could not be read: the list still counts
    stale = []
    for path in folders.values():
        pid = _engine_pid(path) if os.path.isdir(path) else None
        if pid is None:
            continue
        if not engine_current(path, pid)[0]:
            stale.append(path)
    return stale


def engine_current(repo, pid):
    """Is this engine this build's, working on this folder? (True, '') or
    (False, why): 'version' - another version; 'build' - this version, but
    another build of it (its code differs: see engine_fingerprint); 'place' -
    it works on another folder (this one's old place). An engine from before
    2.2.2 names no build and is judged by its version alone."""
    state = daemon_state(repo)
    if state.get("version", "") != APP_VERSION:
        return False, "version"
    build = state.get("build", "")
    if build and build != engine_fingerprint():
        return False, "build"
    if engine_serves(pid, repo) is False:
        return False, "place"
    return True, ""


def unlisted_engines(cfg):
    """Folders with an engine of this copy running that are NOT on the app's
    list - removed from it earlier, say, while the engine ran on. Invisible
    until now: a test project synced like that for a day."""
    listed = {_engine_key(e.get("path", "")) for e in cfg.get("projects", [])
              if e.get("path")}
    found = []
    try:
        for folder in engines_on_machine(engine_script()):
            if (os.path.isdir(folder) and _engine_key(folder) not in listed
                    and daemon_pid(folder) is not None):
                found.append(folder)
    except Exception:
        pass
    return sorted(found, key=lambda f: os.path.basename(f.rstrip("\\/")).lower())


def _engine_key(repo):
    """One key per FOLDER, however it is spelled. A folder reached two ways
    (a junction, a second spelling on the list) is still one worktree, and
    two replacements racing on it would start two engines there."""
    try:
        st = os.stat(repo)
        if st.st_ino:
            return ("id", st.st_dev, st.st_ino)
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(os.path.abspath(repo)))


class Engines:
    """Every engine this app starts, stops or replaces - one record per project.

    The window used to keep ONE engine: the last it had started, whichever
    project that was. Measured 2026-10-02: open A (its engine started here),
    go Home, open B (its engine adopted, already running), press Stop sync -
    and A's engine was killed as well, with nothing on screen to say so. An
    engine belongs to a project, so they are kept by project.

    It also keeps what is under way - a replacement onto this build, or the
    person's stop - so the startup sweep, a window opening that project, and
    Stop never act on one folder at once: two engines started on one worktree
    is the one thing git cannot survive, and a replacement that brings an
    engine back after the person said stop is worse than the stale one.

    Every way an engine ends goes through one routine, _end_one: at rest,
    never in the middle of a git command. An engine that understands a stop
    request (2.2.1 on) is asked, and stops itself at the top of its next
    pass. An older one - or one working on another folder, whose requests are
    read there - is watched from outside and ended in its rest.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._procs = {}        # project -> (the Popen this app started, its folder)
        self._jobs = {}         # project -> a replacement under way
        self._stops = {}        # project -> the person's stop under way
        self._closing = False

    # -- what is running -------------------------------------------------

    def ours(self, repo):
        """The engine this app started for repo, while it runs; else None.
        One started for another folder - this project's old place - is not."""
        with self._lock:
            entry = self._procs.get(_engine_key(repo))
        if entry is None:
            return None
        proc, folder = entry
        if proc.poll() is not None or not _same_folder(folder, repo):
            return None
        return proc

    def replacing(self, repo):
        with self._lock:
            return _engine_key(repo) in self._jobs

    def stopping(self, repo):
        with self._lock:
            return _engine_key(repo) in self._stops

    def running(self, repo):
        """Is an engine covering repo - one started here, one adopted by its
        heartbeat, or one being swapped or stopped right now?"""
        if not repo:
            return False
        return (self.replacing(repo) or self.stopping(repo)
                or self.ours(repo) is not None or daemon_pid(repo) is not None)

    # -- starting ----------------------------------------------------------

    def start(self, repo):
        """Start repo's engine now - it was opened, or left syncing, and none
        runs there. One this app already started there and still running is
        returned instead: the window opening a project and the startup resume
        can reach for the same one at the same moment, and two engines on one
        worktree is the one thing git cannot survive."""
        key = _engine_key(repo)
        with self._lock:
            entry = self._procs.get(key)
            if entry is not None and entry[0].poll() is None and _same_folder(entry[1], repo):
                return entry[0]
            proc = launch_engine(repo)
            self._procs[key] = (proc, repo)
        return proc

    def is_closing(self):
        with self._lock:
            return self._closing

    def claim(self, repo):
        """Reserve repo for one replacement. False when one is already under
        way there, the person is stopping it, or the app is closing."""
        key = _engine_key(repo)
        with self._lock:
            if self._closing or key in self._jobs or key in self._stops:
                return False
            self._jobs[key] = {"cancel": False, "swapping": False, "mode": None}
            return True

    # -- the person's stop -------------------------------------------------

    def stop(self, repo, done=None, wait=None):
        """The person's Stop sync, Disconnect or Change folder, or a project
        taken off the list together with its syncing. Returns at once - False
        when nothing runs there. The engine then ends at rest, on a worker
        thread, and `done(outcome)` is called there: 'stopped', or 'killed'
        when it never came to rest within `wait` seconds and was ended where
        it stood, the way every Stop did before. A replacement under way is
        called off first, so it cannot bring an engine back behind the
        person's back."""
        key = _engine_key(repo)
        if not (self.replacing(repo) or self.ours(repo) is not None
                or _engine_pid(repo) is not None):
            return False
        with self._lock:
            stop = self._stops.get(key)
            if stop is not None:                 # already on its way
                if done is not None:
                    stop["done"].append(done)
                return True
            job = self._jobs.get(key)
            if job is not None:
                job["cancel"] = True
            stop = {"repo": repo, "mode": None, "done": [done] if done else []}
            self._stops[key] = stop
        threading.Thread(target=self._stop_job, daemon=True,
                         args=(repo, key, stop, REPLACE_WAIT if wait is None else wait)).start()
        return True

    def _stop_job(self, repo, key, stop, wait):
        outcome = "none"
        try:
            # A replacement just called off may be in its one step; it ends
            # first, so the two never act on the folder at once.
            with self._lock:
                give_up = time.time() + 30
                while key in self._jobs and time.time() < give_up:
                    self._changed.wait(0.25)
            targets = []
            proc = self.ours(repo)
            if proc is not None:
                targets.append(proc.pid)
            pid = _engine_pid(repo)
            if pid and pid not in targets:
                targets.append(pid)
            killed = False
            for pid in targets:
                if self._end_one(repo, pid, wait, stop, "stop") in ("busy", "failed"):
                    kill_pid(pid)           # the last resort: what every Stop did before
                    killed = True
            if targets:
                outcome = "killed" if killed else "stopped"
                with self._lock:
                    entry = self._procs.get(key)
                    if entry is not None and entry[0].poll() is not None:
                        self._procs.pop(key, None)
                # Ended from outside, an engine cannot remove its heartbeat.
                if daemon_pid(repo) is None:
                    try:
                        os.remove(os.path.join(repo, ".teamsync.lock"))
                    except OSError:
                        pass
        finally:
            with self._lock:
                self._stops.pop(key, None)
                callbacks = list(stop["done"])
                self._changed.notify_all()
            for fn in callbacks:
                try:
                    fn(outcome)
                except Exception:
                    pass

    def stop_now(self, repo):
        """End every engine on repo at once, wherever it stands - the old way,
        kept for the last resort and for tidying up after tests."""
        key = _engine_key(repo)
        with self._lock:
            job = self._jobs.get(key)
            if job is not None:
                job["cancel"] = True
            entry = self._procs.pop(key, None)
        if entry is not None and entry[0].poll() is None:
            kill_pid(entry[0].pid)
        pid = daemon_pid(repo)
        if pid:
            kill_pid(pid)

    # -- the app coming and going -----------------------------------------

    def opened(self):
        with self._lock:
            self._closing = False

    def close(self, wait=10.0):
        """The app is going away. A replacement still waiting for its moment
        lets go - that engine runs on, on its old code, until the next start
        moves it - while one whose old engine is already ending is waited for
        until the new one is started: an engine is never stopped and then
        forgotten, which would end that project's syncing silently. The
        person's stop rides on the engine's own request where it has one, and
        the request outlives the app; where it has none, the stop is finished
        now, the old way."""
        with self._lock:
            self._closing = True
            unfinished = [s["repo"] for s in self._stops.values() if s["mode"] != "request"]
        for repo in unfinished:
            self.stop_now(repo)
        with self._lock:
            give_up = time.time() + wait
            while any(job["swapping"] for job in self._jobs.values()):
                left = give_up - time.time()
                if left <= 0:
                    break
                self._changed.wait(left)

    # -- moving an engine onto this build -----------------------------------

    def replace(self, repo, wait=None, up=None, start_anyway=False):
        """Move repo's live engine onto this build. Blocking - for a worker
        thread, after claim(). `start_anyway`: the person opened the project,
        so if its engine has gone by itself meanwhile, one is started all the
        same. Returns (outcome, old version, detail):

          restarted - the old engine is gone, and the new one's own heartbeat
                      says this version and build; detail says why it was
                      replaced (engine_current: version, build or place)
          started   - (start_anyway) the old one had gone by itself; a new one
                      was started, and says this version
          busy      - it never came to rest within `wait` seconds, so it was
                      left running as it was
          lost      - the old engine was stopped but the new one could not be
                      started: nothing syncs that project now
          died      - the new one stopped at once; slow - it has not reported
          failed    - the old one could not be stopped; it runs on as it was
          none, current, gone, cancelled - nothing to do, nothing done
        """
        key = _engine_key(repo)
        try:
            return self._replace(repo, key, REPLACE_WAIT if wait is None else wait,
                                 ENGINE_UP_WAIT if up is None else up, start_anyway)
        finally:
            with self._lock:
                job = self._jobs.pop(key, None)
                if job is not None:
                    job["swapping"] = False
                self._changed.notify_all()

    def _replace(self, repo, key, wait, up, start_anyway):
        with self._lock:
            job = self._jobs.get(key)
        if job is None:
            return "cancelled", "", ""
        pid = _engine_pid(repo)
        old = daemon_state(repo).get("version", "") if pid else ""
        ended = None
        why = ""
        if pid:
            current, why = engine_current(repo, pid)
            if current:
                return "current", old, ""
            if not os.path.isfile(engine_script()):
                return "failed", old, "this copy's engine file is missing"
            ended = self._end_one(repo, pid, wait, job, "restart")
            if ended in ("cancelled", "busy"):
                return ended, old, ""
            if ended == "failed":
                return "failed", old, "Windows would not stop it"
            if ended == "gone" and not start_anyway:
                return "gone", old, ""          # it stopped by itself: start nothing
        elif not start_anyway:
            return "none", "", ""
        # The old engine is gone. One ended from outside could not tidy up:
        # its heartbeat names a process that no longer exists - removed, as
        # start_sync always has.
        try:
            os.remove(os.path.join(repo, ".teamsync.lock"))
        except OSError:
            pass
        with self._lock:
            try:
                if job["cancel"] or (self._closing and not job["swapping"]):
                    # Stop was pressed while it was being swapped - the person
                    # gets what they asked for, no engine at all - or the app
                    # is going and nothing was stopped here.
                    return "cancelled", old, ""
                try:
                    proc = launch_engine(repo)
                except Exception as exc:
                    return "lost", old, f"Windows refused to start the new one ({exc})"
                self._procs[key] = (proc, repo)
            finally:
                job["swapping"] = False
                self._changed.notify_all()
        done = "restarted" if ended == "ended" else "started"
        # "Replaced" means the new engine says so itself, in its own heartbeat:
        # this version, this build, this folder.
        give_up = time.time() + up
        while time.time() < give_up:
            with self._lock:
                if job["cancel"]:
                    return "cancelled", old, ""
            if proc.poll() is not None:
                return "died", old, f"exit code {proc.returncode}"
            if daemon_pid(repo) == proc.pid and engine_current(repo, proc.pid)[0]:
                return done, old, why
            time.sleep(0.25)
        return "slow", old, ""

    # -- ending one engine, at rest ------------------------------------------

    def _end_one(self, repo, pid, wait, job, mode):
        """End one engine - never in the middle of a git command. `mode` is
        'restart' (a replacement) or 'stop' (the person's). Returns 'ended',
        'gone' (it went by itself), 'cancelled', 'busy' (it never came to rest
        within `wait` seconds and runs on) or 'failed'."""
        k32 = _winapi()["k32"]
        # TERMINATE | SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        handle = k32.OpenProcess(0x0001 | 0x00100000 | 0x1000, 0, int(pid))
        if not handle:
            return "gone"
        try:
            # A handle held pins the id: Windows gives it to no new process
            # until the handle is closed. So if this pid is, right now, the
            # engine the heartbeat vouches for or one this app started, the
            # handle is that engine and nothing else.
            ours = self.ours(repo)
            if _engine_pid(repo) != pid and (ours is None or ours.pid != pid):
                return "gone"
            if (engine_serves(pid, repo) is True and _engine_pid(repo) == pid
                    and daemon_state(repo).get("stop") == "signal"):
                result = self._end_by_request(repo, handle, wait, job, mode)
                if result is not None:
                    return result
            return self._end_at_rest(pid, handle, wait, job, mode)
        finally:
            k32.CloseHandle(handle)

    def _end_by_request(self, repo, handle, wait, job, mode):
        """Ask the engine to stop itself at the top of its next pass, where it
        is between two commands by construction. None when the request could
        not even be written: the caller watches from outside instead."""
        k32 = _winapi()["k32"]
        with self._lock:
            if job.get("cancel") or (self._closing and mode == "restart"):
                return "cancelled"
            if not ask_engine_to_stop(repo, mode):
                return None
            job["mode"] = "request"
            if mode == "restart":
                job["swapping"] = True      # it may go any moment now: close() waits
        give_up = time.time() + wait
        while True:
            if k32.WaitForSingleObject(handle, 100) == 0:
                return "ended"
            with self._lock:
                quit = job.get("cancel") or (self._closing and mode == "restart")
            if not quit and time.time() < give_up:
                continue
            if withdraw_stop_request(repo):
                # Taken back before the engine read it: it runs on as it was.
                if mode == "restart":
                    with self._lock:
                        job["swapping"] = False
                        self._changed.notify_all()
                return "cancelled" if quit else "busy"
            # The engine has taken it and is on its way out: let it finish.
            k32.WaitForSingleObject(handle, 60000)
            return "ended" if k32.WaitForSingleObject(handle, 0) == 0 else "busy"

    def _end_at_rest(self, pid, handle, wait, job, mode):
        """For an engine that cannot be asked: watched from outside, and ended
        in its rest between two passes."""
        k32 = _winapi()["k32"]
        born = process_start(pid)
        give_up = time.time() + wait
        quiet_since = None
        with self._lock:
            job["mode"] = "rest"
        while True:
            with self._lock:
                if job.get("cancel") or self._closing:
                    return "cancelled"
            if k32.WaitForSingleObject(handle, 0) == 0:
                return "gone"
            now = time.time()
            # Stopped only at REST, not merely between two commands. The
            # gaps between the git commands of one pass last milliseconds,
            # and the next command could start in the instant between a look
            # and the stop; the engine's rest between passes lasts a full
            # second (teamsync.ps1: Start-Sleep -Seconds 1), and the first
            # commands after it only read (git rev-parse, git diff
            # --name-only). No command seen for REPLACE_QUIET means rest, with
            # most of the second still to run. And the engine alone is
            # stopped, never a git it started.
            if engine_busy(pid, born):
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= REPLACE_QUIET:
                with self._lock:
                    if job.get("cancel") or self._closing:
                        return "cancelled"
                    if not k32.TerminateProcess(handle, 1):
                        return "failed"
                    if mode == "restart":
                        job["swapping"] = True
                k32.WaitForSingleObject(handle, 10000)
                return "ended"
            if now >= give_up:
                return "busy"
            time.sleep(REPLACE_POLL)


ENGINES = Engines()


ONLINE_SECONDS = 150      # the engine beats every 60 s; this allows two misses


def _is_me(name, mine):
    """Is this ref name one of ours? `mine` may be a name or a set of them.

    One person's machines are one person here, so `mine` carries every name
    this account has ever published under - see my_own_names.
    """
    if isinstance(mine, (set, frozenset, list, tuple)):
        return name in mine
    return name == mine


class GuardError(Exception):
    """A question the destructive check could not get answered.

    Never to be read as "nothing destructive here": the caller keeps what it
    last knew, exactly as the engine holds the publish.
    """


_ZERO_SHA = "0" * 40


def _guard_git(repo, what, *args):
    raw = git_bytes(repo, *args)
    if raw is None:
        raise GuardError(f"git could not answer the destructive check ({what})")
    return raw


def _split_z(raw):
    """git -z output as a list, separators and stray newlines dropped."""
    return [t.lstrip("\r\n") for t in raw.decode("utf-8", "replace").split("\0")
            if t.lstrip("\r\n")]


def _batches(items, limit=8000):
    """Windows caps a command line at 32,767 characters, and a guard that died
    on a large change set would hide exactly the large accidents."""
    batch, size = [], 0
    for it in items:
        if batch and size + len(it) + 3 > limit:
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += len(it) + 3
    if batch:
        yield batch


def _guard_changed(repo, base, letter):
    # --no-renames: a rename deletes the old name for everybody else, whether
    # or not it happened to be staged with `git mv`.
    return _split_z(_guard_git(repo, f"diff {letter} {base}", "diff", "--name-only", "-z",
                               "--no-renames", "--diff-filter=" + letter, base, "--"))


def _guard_versions(repo, rev, paths):
    """{path: every blob the path held anywhere in rev's history}.

    Both sides of every change, and merges too: -c lists a merge whose result
    differs from all of its parents, which is where a hand-made conflict
    resolution lives. One pass for all the paths.
    """
    found = {p: set() for p in paths}
    for batch in _batches(paths):
        tokens = _split_z(_guard_git(
            repo, f"log {rev}", "--literal-pathspecs", "log", rev, "--full-history", "--root",
            "-c", "--raw", "--no-abbrev", "--no-renames", "-z", "--format=", "--", *batch))
        i = 0
        while i < len(tokens):
            meta = tokens[i]
            if meta.startswith(":") and i + 1 < len(tokens):
                # ":<modes> <blobs> <status>", one colon per parent
                parents = len(meta) - len(meta.lstrip(":"))
                fields = meta.lstrip(":").split(" ")
                path = tokens[i + 1]
                if path in found:
                    found[path].update(s for s in fields[parents + 1: 2 * (parents + 1)]
                                       if s != _ZERO_SHA)
                i += 2
            else:
                i += 1
    return found


def _guard_working_blobs(repo, paths):
    """{path: the blob the file on disk would be committed as} - after git's
    own clean filters, exactly what a commit would store."""
    by_full = {}
    for p in paths:
        full = os.path.join(repo, p.replace("/", os.sep))
        if os.path.isfile(full):
            by_full[full] = p
    out = {}
    for batch in _batches(list(by_full)):
        shas = _guard_git(repo, "hash-object", "hash-object", "--", *batch).decode(
            "ascii", "replace").split()
        if len(shas) != len(batch):
            raise GuardError("git hash-object answered for the wrong number of files")
        out.update((by_full[f], s) for f, s in zip(batch, shas))
    return out


def _guard_removed(repo, base):
    """[(path, blob it held at base)] for every path that will be gone."""
    tokens = _split_z(_guard_git(repo, f"diff --raw D {base}", "diff", "--raw", "-z",
                                 "--no-renames", "--no-abbrev", "--diff-filter=D", base, "--"))
    out, i = [], 0
    while i < len(tokens):
        meta = tokens[i]
        if meta.startswith(":") and i + 1 < len(tokens):
            # ":<old mode> <new mode> <old blob> <new blob> D"
            out.append((tokens[i + 1], meta.lstrip(":").split(" ")[2]))
            i += 2
        else:
            i += 1
    return out


def _guard_arrivals(repo, bases):
    """The blob of every file that will exist after this publish but was not
    there at one of `bases`: added and tracked, or new on the disk and not
    ignored - what `git add -A` is about to take in. A file that disappears
    while its exact content arrives here was MOVED."""
    paths = set()
    for b in bases:
        paths.update(_guard_changed(repo, b, "A"))
    paths.update(_split_z(_guard_git(repo, "ls-files --others", "ls-files", "-z",
                                     "--others", "--exclude-standard")))
    return set(_guard_working_blobs(repo, sorted(paths)).values()) if paths else set()


def destructive_changes(repo):
    """What is waiting to go out that would DESTROY work, not add to it.

    The window's copy of the rule the engine applies before every publish
    (the "destructive guard" block in sync-core.ps1, read its comment for the
    reasoning). test_destructive_agreement holds the copies to one answer.

      deleted  - a file the team has, or this machine had, that will no
                 longer exist once this is published - unless its exact
                 content arrives under a new name in the same publish: a
                 move destroys nothing and is not asked about (the user's
                 ruling, 2026-10-01); moved AND changed still is
      reverted - a file whose content is byte-identical to an EARLIER
                 version of itself - every earlier version, not the last forty

    "Waiting to go out" is everything not yet on the shared branch: work on
    the disk AND work already committed here but not sent, measured from the
    merge base, so a machine that is merely behind is never caught.

    A handful of git calls whatever the size: the old shape asked once per
    version of every file - ~250 processes for 14 files of a real project, on
    the window's own thread, every four seconds - and the window never
    answered again. Raises GuardError when git cannot answer.
    """
    head = git_bytes(repo, "rev-parse", "-q", "--verify", "HEAD")
    if not head:
        return {"deleted": [], "reverted": []}
    head = head.decode("ascii", "replace").strip()
    base = git_bytes(repo, "merge-base", "HEAD", "refs/remotes/origin/main")
    base = base.decode("ascii", "replace").strip() if base else ""
    base = base or head

    # gone, with the content each one held - from this machine's last commit
    # and (below) from what the team has
    removed = {}
    for p, blob in _guard_removed(repo, "HEAD"):
        removed.setdefault(p, set()).add(blob)
    # changed on the disk since this machine's last commit: judged against
    # every version this machine has ever held
    uncommitted = list(_guard_changed(repo, "HEAD", "M"))
    # committed here, not yet sent: judged against every version the TEAM has
    # held, so an edit that is merely unpublished is never taken for a
    # roll-back of itself
    committed_only = []
    if base != head:
        for p, blob in _guard_removed(repo, base):
            removed.setdefault(p, set()).add(blob)
        readded = set(_guard_changed(repo, "HEAD", "A"))
        seen = set(uncommitted)
        for p in _guard_changed(repo, base, "M"):
            if p in seen:
                continue
            seen.add(p)
            # removed by a local commit and then put back on the disk: that is
            # uncommitted work like any other edit
            (uncommitted if p in readded else committed_only).append(p)

    now = _guard_working_blobs(repo, uncommitted + committed_only)
    reverted = set()
    for rev, group in (("HEAD", uncommitted), (base, committed_only)):
        group = [p for p in group if p in now]
        if group:
            hist = _guard_versions(repo, rev, group)
            reverted.update(p for p in group if now[p] in hist.get(p, ()))

    # excused only when EVERY version a deletion takes away arrives intact
    # under another name: moved, not destroyed
    deleted = set()
    if removed:
        arrived = _guard_arrivals(repo, ["HEAD"] + ([base] if base != head else []))
        deleted = {p for p, blobs in removed.items() if not blobs <= arrived}

    return {"deleted": sorted(deleted, key=_ordinal),
            "reverted": sorted(reverted, key=_ordinal)}


def _ordinal(text):
    """The order .NET's StringComparer.Ordinal uses: UTF-16 code units.

    Python's own sorted() orders by code point. The two agree except where a
    character outside the basic plane (an emoji) meets one in U+E000-U+FFFF
    (full-width forms, private use) - and there they disagree, so a signature
    the window computed could never match the engine's, and a confirmed
    accident would stay held for ever. Sorting by code units removes the case.
    """
    return text.encode("utf-16-be")


def destructive_signature(changes):
    """Identifies one particular accident, so a confirmation covers only it.

    Ordinal sort and UPPERCASE hex to match the engine byte for byte - the
    engine is PowerShell, whose default sort is culture-aware, so both sides
    say ordinal explicitly.
    """
    import hashlib
    paths = sorted(list(changes.get("deleted", [])) + list(changes.get("reverted", [])),
                   key=_ordinal)
    if not paths:
        return ""
    text = "\n".join(paths)
    return hashlib.sha1(text.encode("utf-8")).hexdigest().upper()[:12]


FILE_LIMIT = 25 * 1024 * 1024    # one file this big, going out for the first time, asks
GROUP_LIMIT = 50 * 1024 * 1024   # so does a new item that adds this much in all
_TEAM_TREE = {}                  # repo -> (shared branch's version, what the team has there)


def _team_tree(repo, rev):
    """What the team has at rev: its folders, each file's content id, and the
    size of each content id. Read once per version of the shared branch - it
    changes only when somebody publishes. Mirrors Get-DGTeamTree."""
    cached = _TEAM_TREE.get(repo)
    if cached and cached[0] == rev:
        return cached[1]
    dirs, files, sizes = set(), {}, {}
    if rev:
        for t in _split_z(_guard_git(repo, f"ls-tree {rev}", "ls-tree", "-r", "-t", "-l", "-z", rev)):
            # "<mode> <type> <id> <size>\t<path>"
            meta, tab, path = t.partition("\t")
            if not tab:
                continue
            f = meta.split()
            if f[1] == "tree":
                dirs.add(path)
            elif f[1] == "blob":
                files[path] = f[2]
                sizes[f[2]] = int(f[3])
    tree = {"dirs": dirs, "files": files, "sizes": sizes}
    _TEAM_TREE[repo] = (rev, tree)
    return tree


def _guard_committed(repo, tip, upstream):
    """Every file version inside commits reachable from tip that the team does
    not have - what a push of tip carries besides the disk. Each content id
    once, under the first name git reports for it. Mirrors Get-DGCommitted."""
    rng = [tip, "--not", upstream] if upstream else [tip]
    raw = _guard_git(repo, "rev-list --objects", "rev-list", "--objects", *rng, "--")
    named = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        # "<id> <path>"; a commit has no path, the top folder an empty one
        line = line.rstrip("\r")
        sp = line.find(" ")
        if 0 < sp < len(line) - 1:
            named.append((line[:sp], line[sp + 1:]))
    if not named:
        return []
    try:
        out = subprocess.run(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            cwd=repo, input=("\n".join(sha for sha, _ in named) + "\n").encode("ascii"),
            capture_output=True, creationflags=CREATE_NO_WINDOW, env=_GIT_ENV)
    except Exception:
        out = None
    info = out.stdout.decode("ascii", "replace").splitlines() if out else []
    if out is None or out.returncode != 0 or len(info) != len(named):
        raise GuardError("git could not answer the destructive check (cat-file sizes)")
    pieces = []
    for (_, path), line in zip(named, info):
        f = line.split(" ")
        if len(f) >= 3 and f[1] == "blob":
            pieces.append({"path": path, "blob": f[0], "size": int(f[2]), "committed": True})
    return pieces


def large_outgoing(repo, tip="HEAD", committed_only=False):
    """Large content about to leave: [{item, bytes, files, shared, committed}],
    ordinal by item.

    The window's copy of the large-content rule in the guard block of
    sync-core.ps1 (read its comment there): what the upload really carries -
    files on the disk, AND file versions inside commits made here and not yet
    sent - minus content the team already has, filed under the outermost
    folder the team does not have, or else the file itself. Held when one file
    in an item is 25 MB or more, or the item adds 50 MB or more.
    test_large_guard holds the copies to one answer. Raises GuardError when
    git cannot answer.
    """
    tip_id = git(repo, "rev-parse", "-q", "--verify", tip + "^{commit}")
    up_id = git(repo, "rev-parse", "-q", "--verify", "refs/remotes/origin/main^{commit}")
    pieces = _guard_committed(repo, tip_id, up_id) if tip_id else []
    if not committed_only:
        disk = set(_split_z(_guard_git(repo, "ls-files --others", "ls-files", "-z", "--others",
                                       "--exclude-standard")))
        if tip_id:
            for letter in ("A", "M"):
                disk.update(_guard_changed(repo, "HEAD", letter))
        else:
            # nothing committed yet: everything staged goes in the first commit
            disk.update(_split_z(_guard_git(repo, "ls-files", "ls-files", "-z")))
        for p in disk:
            full = os.path.join(repo, p.replace("/", os.sep))
            try:
                if os.path.isfile(full):
                    pieces.append({"path": p, "blob": None, "size": os.path.getsize(full),
                                   "committed": False})
            except (OSError, ValueError):
                continue
    if not pieces:
        return []

    team = _team_tree(repo, up_id)
    items, under = {}, {}
    for pc in pieces:
        path = pc["path"]
        shared = path in team["files"]
        item = path
        cut = path.rfind("/")
        if not shared and cut > 0:
            # the outermost folder the team does not have - once per folder
            folder = path[:cut]
            if folder not in under:
                owner, acc = "", ""
                for part in folder.split("/"):
                    acc = acc + "/" + part if acc else part
                    if acc not in team["dirs"]:
                        owner = acc + "/"
                        break
                under[folder] = owner
            if under[folder]:
                item = under[folder]
        items.setdefault(item, {"item": item, "shared": shared, "pieces": []})["pieces"].append(pc)

    def measure(it):
        total = biggest = 0
        paths, committed = set(), False
        for pc in it["pieces"]:
            if pc.get("known"):
                continue
            total += pc["size"]
            biggest = max(biggest, pc["size"])
            paths.add(pc["path"])
            committed = committed or pc["committed"]
        return total, (biggest >= FILE_LIMIT or total >= GROUP_LIMIT), len(paths), committed

    candidates = [it for it in items.values() if measure(it)[1]]
    if not candidates:
        return []

    # Already there, or already on its way. A file is read (hashed) only when
    # that could change the answer: its size matches something known, AND
    # without it the item would no longer be large.
    known_ids = set(team["sizes"])
    known_sizes = set(team["sizes"].values())
    for pc in pieces:
        if pc["committed"]:
            known_ids.add(pc["blob"])
            known_sizes.add(pc["size"])
    to_hash = []
    for it in candidates:
        rest = rest_big = 0
        maybe = []
        for pc in it["pieces"]:
            if pc["committed"]:
                if pc["blob"] in team["sizes"]:
                    pc["known"] = True
                    continue
            elif pc["size"] in known_sizes:
                maybe.append(pc["path"])
                continue
            rest += pc["size"]
            rest_big = max(rest_big, pc["size"])
        if maybe and rest_big < FILE_LIMIT and rest < GROUP_LIMIT:
            to_hash.extend(maybe)
    if to_hash:
        now = _guard_working_blobs(repo, to_hash)
        for it in candidates:
            for pc in it["pieces"]:
                if not pc["committed"] and now.get(pc["path"]) in known_ids:
                    pc["known"] = True

    held = []
    for it in candidates:
        total, big, files, committed = measure(it)
        if big:
            held.append({"item": it["item"], "bytes": total, "files": files,
                         "shared": it["shared"], "committed": committed})
    return sorted(held, key=lambda h: _ordinal(h["item"]))


def large_signature(large):
    """One particular set of large items, so "send them" covers only those."""
    import hashlib
    items = sorted((h["item"] for h in large or []), key=_ordinal)
    if not items:
        return ""
    return hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest().upper()[:12]


def approve_large(repo, large):
    git(repo, "config", "--local", "teamsync.largeok", large_signature(large))


def gitignore_line(path):
    """One exact path as a .gitignore line: anchored to the project root, so it
    names this item and never a same-named one elsewhere, with git's pattern
    characters escaped so "[draft].md" means itself. Mirrors
    Format-GitIgnoreLine in sync-core.ps1."""
    escaped = re.sub(r"([\[\]*?\\])", r"\\\1", path)
    if escaped.startswith(("#", "!")):
        escaped = "\\" + escaped
    return "/" + escaped


def add_gitignore_lines(root, lines):
    """APPEND the lines .gitignore lacks - its own lines, blank lines and
    comments are its owners' and are never rewritten - in the line ending the
    file already uses. Mirrors Add-GitIgnoreLines in sync-core.ps1."""
    path = os.path.join(root, ".gitignore")
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        text = ""
    have = set(re.split(r"\r?\n", text))
    nl = "\r\n" if ("\r\n" in text or not text) else "\n"
    add = []
    for line in lines:
        if line and line not in have and line not in add:
            add.append(line)
    if not add:
        return
    if text and not text.endswith("\n"):
        text += nl
    text += nl.join(add) + nl
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def keep_large_home(repo, large):
    """"Keep them home": into .gitignore, so they are never sent - and the
    .gitignore travels, so no other machine sends its own copy either.

    Only NEW items: keeping home a file the team already has would take it out
    of the project for everybody. Something already inside a commit made here
    would still travel - a push sends commits, not the folder's final state -
    so the unsent commits are folded back into the next one first; every file
    stays exactly as it is on the disk. Mirrors Keep-LargeHome in sync-core.ps1.
    Raises GuardError when that cannot be done safely.
    """
    new = [h for h in large if not h.get("shared")]
    if any(h.get("committed") for h in new):
        gd = git(repo, "rev-parse", "--git-dir")
        gd = gd if os.path.isabs(gd) else os.path.join(repo, gd)
        if not gd or any(os.path.exists(os.path.join(gd, n))
                         for n in ("rebase-merge", "rebase-apply", "MERGE_HEAD")):
            raise GuardError("a merge is still being finished here - try again once it is done")
        base = git(repo, "merge-base", "HEAD", "refs/remotes/origin/main")
        if not base:
            raise GuardError("it is already in this folder's own history, which goes up as it is")
        done = subprocess.run(["git", "reset", "-q", "--soft", base], cwd=repo,
                              capture_output=True, creationflags=CREATE_NO_WINDOW)
        if done.returncode != 0:
            raise GuardError("git could not set the unsent commits aside")
    add_gitignore_lines(repo, [gitignore_line(h["item"]) for h in new])
    for h in new:
        git(repo, "--literal-pathspecs", "rm", "-r", "-q", "--cached", "--ignore-unmatch",
            "--", h["item"].rstrip("/"))
    git(repo, "config", "--local", "--unset", "teamsync.largeok")


def read_destructive(repo):
    """Everything waiting for the person's word - (what would be destroyed,
    its confirmed signature, large new content, its confirmed signature) -
    git only, no widgets, so it is safe on a worker thread. Raises GuardError
    when git cannot answer."""
    changes = destructive_changes(repo)
    approved = git(repo, "config", "--local", "--get", "teamsync.destructiveok")
    large = large_outgoing(repo)
    large_ok = git(repo, "config", "--local", "--get", "teamsync.largeok")
    return changes, approved, large, large_ok


def approve_destructive(repo, changes):
    """The person said they meant it. Recorded against THIS set only."""
    git(repo, "config", "--local", "teamsync.destructiveok",
        destructive_signature(changes))


def restore_destructive(repo, changes):
    """The other answer: put them back.

    This is the undo the app never had. Every version has always been in every
    clone - there was simply no door to it that did not require knowing git.

    Where each file comes back FROM depends on where the damage sits. Still on
    the disk: this machine's own newest version (HEAD). Already committed
    here: the version the team has (the merge base), because HEAD itself is
    the damage. A plain `git checkout -- <file>` restored from the index, so a
    deletion staged with `git rm` could not be undone at all. Mirrors
    Restore-Destructive in sync-core.ps1.
    """
    base = git(repo, "merge-base", "HEAD", "refs/remotes/origin/main") or "HEAD"
    for f in list(changes.get("deleted", [])) + list(changes.get("reverted", [])):
        if not f:
            continue
        in_head = git_bytes(repo, "cat-file", "-e", "HEAD:" + f) is not None
        dirty = True
        if in_head:
            # --quiet answers through the exit code: 1 = differs, which
            # git_bytes reports as None.
            dirty = git_bytes(repo, "--literal-pathspecs", "diff", "--quiet",
                              "HEAD", "--", f) is None
        source = "HEAD" if (in_head and dirty) else base
        git(repo, "--literal-pathspecs", "checkout", source, "--", f)
    git(repo, "config", "--local", "--unset", "teamsync.destructiveok")


def name_logins(repo):
    """Which GitHub account registered each name: {name: login}.

    Published by the engine as refs/teamsync/identity/<name>/<login>. This is
    the only thing on the wire that can say two different names are one
    person, and until 2.1.4 nothing read it.
    """
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/identity")
    logins = {}
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) < 5:
            continue
        name, login = parts[3], parts[4]
        if name and login:
            logins[name] = login
    return logins


_NUMBERED = re.compile(r"^(.+)-(\d+)$")


def person_groups(names, logins):
    """Collapse names that are one person. Returns {name: group key}.

    A name is not a person; an ACCOUNT is. Two facts can join two names:

    1. They carry the same login. Certain, and the rule going forward.
    2. One is `X-<n>` and the other is `X`, and both are present. Older
       versions numbered a machine when they mistook the heartbeat their own
       previous run had left behind for a live second machine - so these pairs
       exist on real projects and were never anybody's second machine. Joined
       UNLESS both names carry logins that differ, which proves two accounts
       and outranks the pattern.

    Anything unregistered and unpaired stays itself: we cannot prove a person
    is somebody else, so we never guess them away.
    """
    names = list(names)
    parent = {n: n for n in names}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_login = {}
    for n in names:
        lg = logins.get(n)
        if lg:
            by_login.setdefault(lg, []).append(n)
    for group in by_login.values():
        for other in group[1:]:
            union(group[0], other)

    present = set(names)
    for n in names:
        m = _NUMBERED.match(n)
        if not m:
            continue
        base = m.group(1)
        if base not in present:
            continue
        a, b = logins.get(n), logins.get(base)
        if a and b and a != b:
            continue                      # two accounts - the pattern is a coincidence
        union(base, n)

    return {n: find(n) for n in names}


def team_presence(repo, my_name):
    """Everyone else on this project, freshest beat first.

    The engine publishes a heartbeat as a ref named
    refs/teamsync/presence/<name>/<unix-seconds> and fetches everybody's. A
    ref carries no date, so the timestamp lives in the name.

    Reading was the only part of this that ever assumed two people: the refs
    have always been one-per-person, and it costs the same to read sixty of
    them as two (measured: 55 ms at two people, 59 ms at sixty - which is the
    cost of starting git at all, not of the refs).

    Returns a list of dicts: {name, ago, online}, sorted online-first and then
    by how recently each was seen. A person who beat twice - which happens
    while an old beat is being swept - is counted once, at their freshest.
    """
    import time
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/presence")
    freshest = {}
    for line in out.splitlines():
        # refs/teamsync/presence/<name>/<machine>/<ts>  (this version)
        # refs/teamsync/presence/<name>/<ts>            (before it)
        # The timestamp is the LAST segment either way, which is what lets one
        # reader serve a team that is halfway through an upgrade.
        parts = line.strip().split("/")
        if len(parts) < 5:
            continue
        name = parts[3]
        if not name:
            continue
        try:
            ts = int(parts[-1])
        except ValueError:
            continue
        if ts > freshest.get(name, -1):
            freshest[name] = ts

    # A name is not a person. Names this account retired - and names an older
    # version invented for a machine that never existed - are claimed by no
    # live engine, so nothing sweeps their beats and every one of them used to
    # be listed as a separate teammate for ever.
    groups = person_groups(freshest, name_logins(repo))
    per_person = {}
    for name, ts in freshest.items():
        key = groups[name]
        best = per_person.get(key)
        if best is None or ts > best[1]:
            per_person[key] = (name, ts)          # the name that is actually alive

    # Show the person's PLAIN name where we have it. A trailing number was
    # never part of anybody's name: older versions added it when they mistook
    # their own leftover heartbeat for a second machine, so a name like
    # "someone-2" names a machine that never existed. If the plain form is in the same
    # person's group, that is the name to put on the screen - the number is an
    # artefact, and it stops being shown the moment we can prove it is one.
    for key, (name, ts) in list(per_person.items()):
        m = _NUMBERED.match(name)
        if not m:
            continue
        base = m.group(1)
        if groups.get(base) == key:
            per_person[key] = (base, ts)

    mine_keys = {groups[n] for n in freshest if _is_me(n, my_name)}
    now = int(time.time())
    people = [{"name": n, "ago": max(0, now - ts), "online": (now - ts) <= ONLINE_SECONDS}
              for key, (n, ts) in per_person.items() if key not in mine_keys]
    people.sort(key=lambda p: (not p["online"], p["ago"], p["name"].lower()))
    return people


def partner_presence(repo, my_name):
    """The single freshest other person, as the old two-person display wanted.

    Kept because several callers still speak of one partner; it is now just
    the head of the list.
    """
    people = team_presence(repo, my_name)
    if not people:
        return None, None
    return people[0]["name"], people[0]["ago"]


def sanitise_name(text):
    """The one spelling of a person's name that every layer agrees on.

    The engine publishes presence under this form, and a commit's author name
    put through it lands on the same string - measured on a live project: an
    author written "Ali Reza" and a presence name "Ali-Reza" are one person.
    Author EMAIL is deliberately not the key: measured on the live project,
    one person commits under an address belonging to somebody else entirely,
    so email answers the wrong question.
    """
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-")


# One walk of the last week answers all three windows, so the cascade costs a
# single git call. Cached because it feeds a display that redraws every four
# seconds while the answer changes on the scale of minutes.
_activity_cache = {"repo": None, "at": 0, "value": {}}
ACTIVITY_WINDOWS = (3 * 3600, 24 * 3600, 7 * 86400)


def recent_activity(repo, max_age=60):
    """How much work each person has landed lately, per time window.

    Returns {name: (count_3h, count_24h, count_7d)} keyed by the sanitised
    name, so it joins straight onto presence.

    Cost, measured on a repository of 50,000 commits spread over two years:
    the windowed walk takes ~57 ms, the same as the `git status` this app
    already runs every few seconds, because `--since` stops walking once it
    is past the cutoff. Walking the whole history instead takes 360 ms, which
    is why the window is never widened to "all time".

    "Pushes" cannot be counted from a clone - a push leaves no record a
    clone can see, only the commits it carried. Commits are the honest
    substitute, and for this app they are nearly the same thing: the engine
    publishes what it commits, within minutes.
    """
    import time
    now = time.time()
    if (_activity_cache["repo"] == repo
            and now - _activity_cache["at"] < max_age):
        return _activity_cache["value"]
    out = git(repo, "log", "--all", f"--since={ACTIVITY_WINDOWS[-1]} seconds ago",
              "--format=%at %an")
    counts = {}
    for line in out.splitlines():
        stamp, _, who = line.strip().partition(" ")
        try:
            age = now - int(stamp)
        except ValueError:
            continue
        name = sanitise_name(who)
        if not name:
            continue
        row = counts.setdefault(name, [0, 0, 0])
        for i, window in enumerate(ACTIVITY_WINDOWS):
            if age <= window:
                row[i] += 1
    counts = {n: tuple(v) for n, v in counts.items()}
    _activity_cache.update({"repo": repo, "at": now, "value": counts})
    return counts


def rank_by_activity(names, activity):
    """Order people by recent work, widening the window only as far as needed.

    The rule, in the owner's words: take the busiest of the last three hours;
    if that leaves the list short because everyone else did nothing, fill the
    rest from the last day, then from the last week. Nobody appears twice,
    and the widening never re-orders those already placed - a person who was
    busy in the last three hours outranks yesterday's hardest worker, which
    is the whole point of asking about three hours first.

    People with no commits in any window come last, alphabetically, so the
    list is stable rather than arbitrary. They are never dropped: the full
    list has to be able to reach everybody.
    """
    ordered, placed = [], set()
    for i in range(len(ACTIVITY_WINDOWS)):
        tier = [n for n in names
                if n not in placed and activity.get(n, (0, 0, 0))[i] > 0]
        tier.sort(key=lambda n: (-activity[n][i], n.lower()))
        ordered.extend(tier)
        placed.update(tier)
    ordered.extend(sorted((n for n in names if n not in placed), key=str.lower))
    return ordered


def team_pending_files(repo, my_name):
    """Who has hands on which files right now, decoded from the pending refs.

    The engine keeps refs/teamsync/pending/<name>/<hex-of-path> fetched; this
    reads and decodes them, KEEPING the name. With two people the name could
    be dropped - there was only one other person it could be - and the old
    reader did drop it. With five, "somebody is editing this" is not an
    answer to the question being asked.

    Returns {name: [files]}, names sorted, files sorted within each.
    """
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/pending")
    by_person = {}
    for line in out.splitlines():
        # <ns>/<name>/<machine>/<hex>, or <ns>/<name>/<hex> from an older
        # version. The path is the LAST segment either way - reading position
        # 4 would decode the machine id as a filename and drop the real one.
        parts = line.strip().split("/")
        if len(parts) < 5 or not parts[3] or _is_me(parts[3], my_name):
            continue
        try:
            path = bytes.fromhex(parts[-1]).decode("utf-8", "replace")
        except ValueError:
            continue
        by_person.setdefault(parts[3], set()).add(path)
    return {n: sorted(f) for n, f in sorted(by_person.items(), key=lambda kv: kv[0].lower())}


def team_conflicts(repo, my_name):
    """Who is untangling a conflict right now, and in which files.

    A conflict lives on one machine: everybody else's repository is fine and
    the shared branch is a consistent state, so nothing about this stops
    anyone working. What it buys them is knowledge - adding more changes to a
    file somebody is mid-way through resolving is how the next conflict gets
    made.

    Returns {name: [files]}, names sorted, files sorted within each.
    """
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/conflict")
    by_person = {}
    for line in out.splitlines():
        # <ns>/<name>/<machine>/<hex>, or <ns>/<name>/<hex> from an older
        # version. The path is the LAST segment either way - reading position
        # 4 would decode the machine id as a filename and drop the real one.
        parts = line.strip().split("/")
        if len(parts) < 5 or not parts[3] or _is_me(parts[3], my_name):
            continue
        try:
            path = bytes.fromhex(parts[-1]).decode("utf-8", "replace")
        except ValueError:
            continue
        by_person.setdefault(parts[3], set()).add(path)
    return {n: sorted(f) for n, f in sorted(by_person.items(), key=lambda kv: kv[0].lower())}


def _ref_hex(text):
    return text.encode("utf-8").hex()


def conflict_volunteers(repo):
    """Who has put their hand up for which conflict.

    One person volunteering is the whole point: with five people reading the
    same list, two of them settling the same file separately is not help, it
    is a second conflict. So the claim is public and exclusive - everybody
    else sees the volunteer's name instead of a button.

    Travels as a ref like everything else here:
      refs/teamsync/volunteer/<owner>/<hex-path>/<volunteer>

    Returns {(owner, path): volunteer}.
    """
    out = {}
    for line in (git(repo, "for-each-ref", "--format=%(refname)",
                     "refs/teamsync/volunteer") or "").splitlines():
        # refs / teamsync / volunteer / owner / hexpath / volunteer = 6 parts.
        parts = line.strip().split("/", 5)
        if len(parts) < 6:
            continue
        owner, hexpath, volunteer = parts[3], parts[4], parts[5]
        try:
            path = bytes.fromhex(hexpath).decode("utf-8", "replace")
        except ValueError:
            continue
        if owner and path and volunteer:
            out[(owner, path)] = volunteer
    return out


def set_volunteer(repo, owner, path, me, volunteering):
    """Put our hand up for somebody's conflict, or take it back down.

    Pushed straight to the shared repository rather than left for the engine:
    a claim that arrives four minutes later is not a claim, it is a race.
    Returns True when GitHub took it.
    """
    ref = f"refs/teamsync/volunteer/{owner}/{_ref_hex(path)}/{me}"
    # Pointed at the shared branch's tip, never at HEAD: only the name
    # carries the claim, and HEAD can hold commits not yet sent - pushing it
    # uploaded them past the publish guards (Get-MarkerSource in sync-core).
    src = "refs/remotes/origin/main"
    if volunteering and not git(repo, "rev-parse", "-q", "--verify", src):
        return False
    args = (["push", "-q", "origin", f"{src}:{ref}"] if volunteering
            else ["push", "-q", "origin", f":{ref}"])
    out = subprocess.run(["git"] + args, cwd=repo, capture_output=True,
                         creationflags=CREATE_NO_WINDOW)
    if out.returncode == 0:
        git(repo, "fetch", "-q", "--prune", "origin",
            "+refs/teamsync/volunteer/*:refs/teamsync/volunteer/*")
    return out.returncode == 0


def conflict_report(repo, who, path):
    """The three versions of one teammate's conflicted file, for anybody.

    A conflict happens in one person's working tree, but the material of it
    does not have to stay there. Two of the three versions are in every
    clone already - THEIRS is the shared branch, BASE is the common start -
    and the engine pushes the third, the stuck person's own side, as
    refs/teamsync/conflictwork/<name>. So this can be assembled by any
    teammate without touching their machine.

    What another person CANNOT do is finish somebody else's rebase: that
    lives in their git directory. What they CAN do is decide the final text
    and publish it, which is the useful half.

    Returns {"mine": ..., "theirs": ..., "base": ...}; a value is None when
    that side does not exist (a file one person added, for instance).
    """
    work = f"refs/teamsync/conflictwork/{who}"
    tip = git(repo, "rev-parse", "--verify", "-q", work)
    branch = "origin/main"
    out = {"mine": None, "theirs": None, "base": None}
    if not tip:
        return out
    base = git(repo, "merge-base", tip, branch)
    for key, rev in (("mine", tip), ("theirs", branch), ("base", base)):
        if not rev:
            continue
        # show writes nothing and fails when the path is absent on that side,
        # which is a real answer - "this side has no version of this file".
        text = git(repo, "show", f"{rev}:{path}")
        out[key] = text if text else None
    return out


def write_conflict_report(repo, who, path, folder):
    """Lay one teammate's conflict out as files, the same shape as our own.

    Named from the whole path with folders joined by __, so two files of the
    same name in different folders cannot overwrite each other - the same
    rule the engine's own export follows.
    """
    sides = conflict_report(repo, who, path)
    flat = path.replace("\\", "__").replace("/", "__")
    stem, dot, ext = flat.rpartition(".")
    stem = stem or flat
    ext = (dot + ext) if dot else ""
    os.makedirs(folder, exist_ok=True)
    written = {}
    for key, label in (("mine", "THEIRS"), ("theirs", "SHARED"), ("base", "BASE")):
        # From the READER's point of view the stuck person's side is
        # "theirs" - calling it MINE here would name it after somebody who is
        # not in the room.
        body = sides.get(key)
        target = os.path.join(folder, f"{stem}.{label}{ext}")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body if body is not None
                     else "<< this side has no version of this file >>\n")
        written[label] = target
    note = os.path.join(folder, "CONFLICT.md")
    with open(note, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            f"# {who} is stuck on {path}\n\n"
            f"This is their conflict, read from GitHub. Their own machine is the\n"
            f"only one that can finish their merge - but you can decide what the\n"
            f"file should say and publish it yourself, and their side will take\n"
            f"your version when it arrives.\n\n"
            f"- `{stem}.THEIRS{ext}` - {who}'s version, not yet published anywhere else\n"
            f"- `{stem}.SHARED{ext}` - what is on the shared branch right now\n"
            f"- `{stem}.BASE{ext}` - the version both of those started from\n\n"
            f"## To resolve it for them\n\n"
            f"Edit `{path}` in the project until it carries every intent you find\n"
            f"above, then press Publish now. Nothing here is a lock: if they finish\n"
            f"first, theirs lands and this is wasted work, not damage.\n")
    written["CONFLICT.md"] = note
    return written


def partner_pending_files(repo, my_name):
    """Every file anybody else has hands on, without saying who.

    Kept for the places that only need to know whether anything at all is in
    other hands.
    """
    files = set()
    for paths in team_pending_files(repo, my_name).values():
        files.update(paths)
    return sorted(files)


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
    return sanitise_name(git(repo, "config", "user.name")
                         or os.environ.get("USERNAME", ""))


def my_machine_id(repo):
    """This clone's own machine token, as the engine records it."""
    return git(repo, "config", "--local", "teamsync.machine")


def my_own_names(repo):
    """Every name THIS machine publishes, or has published, on this project.

    Normally just one. But when the engine numbers a second machine it keeps
    the name it came from, and a beat left behind by the previous run can
    outlive it by a couple of minutes - so without this a person sees
    themselves listed as a teammate, with a last-seen time from their own
    last session. Measured on the user's own screen: "amin" appeared beside
    "amin-2", both of them him.
    """
    names = {presence_name(repo)}
    came = git(repo, "config", "--local", "teamsync.renamedfrom")
    if came:
        names.add(sanitise_name(came))
    names = {n for n in names if n}
    if not names:
        return names

    # And every name this ACCOUNT has ever published under, which the local
    # config cannot know: a name retired on a previous install, or one an old
    # version invented, still carries our login in its identity ref. Without
    # this the person sees their own ghost as a teammate holding a file or
    # stuck in a conflict - measured on the user's own screen as "amin-2".
    logins = name_logins(repo)
    known = set(logins) | names
    out = git(repo, "for-each-ref", "--format=%(refname)", "refs/teamsync/presence")
    for line in out.splitlines():
        parts = line.strip().split("/")
        if len(parts) >= 5 and parts[3]:
            known.add(parts[3])
    groups = person_groups(known, logins)
    mine_keys = {groups[n] for n in names if n in groups}
    return {n for n, key in groups.items() if key in mine_keys} | names


def read_partner_state(repo):
    """Everything the team corner and the 'Right now' column show, read from
    git. No widget is touched, so it runs on a worker thread."""
    me = my_own_names(repo)
    people = team_presence(repo, me)
    # Only asked for when it could change the answer - the ranking exists to
    # choose ten out of more than ten, and it caches for a minute anyway. A
    # four-person project never pays for it.
    activity = (recent_activity(repo)
                if len([p for p in people if p["online"]]) > TeamPanel.HOVER_MAX
                else {})
    return {"me": me, "people": people, "activity": activity,
            "conflicts": team_conflicts(repo, me),
            "pending": team_pending_files(repo, me)}


def read_project_state(repo):
    """One look at a project for the window's four-second tick: the team,
    an open conflict, and what is waiting to be sent. Worker-thread safe."""
    snap = read_partner_state(repo)
    snap["unmerged"] = git(repo, "diff", "--name-only", "--diff-filter=U")
    snap["ahead"] = git(repo, "rev-list", "--count", "origin/main..HEAD") or "0"
    snap["net"] = daemon_state(repo).get("net")
    return snap


# The engine's conflict exports under _conflicts: named by their moment, with
# -02, -03 for a second that already had one (New-ExportFolder in
# sync-core.ps1). Crossed-edits keepsakes (-crossed) and teammates' reports
# saved from the Conflicts window (<name>-report) live beside them.
_EXPORT_NAME = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-(\d+))?")


def newest_conflict_export(repo):
    """The folder of the newest conflict export, or None.

    It used to be the last folder of ALL in name order - whatever sorted last:
    a teammate's report saved here (letters sort after digits), or a
    crossed-edits keepsake. So the conflict button opened those instead of
    the copies of the conflict at hand. push-now keeps the same rule."""
    croot = os.path.join(repo, "_conflicts")
    try:
        names = os.listdir(croot)
    except OSError:
        return None
    best, key = None, None
    for name in names:
        m = _EXPORT_NAME.fullmatch(name)
        if not m or not os.path.isdir(os.path.join(croot, name)):
            continue
        k = (name[:19], int(m.group(1) or 1))
        if key is None or k > key:
            best, key = name, k
    return os.path.join(croot, best) if best else None


def seen_phrase(ago_seconds):
    """When the partner was last here, said the way a person would.

    Under an hour, elapsed time reads naturally ("seen 45m ago"). Past an
    hour, arithmetic stops being kind - "seen 1h 5m ago" makes the reader
    compute - so it switches to the clock: today at 14:30, yesterday at
    14:30, or the date for anything older.
    """
    if ago_seconds < 3600:
        return "seen " + humanise(ago_seconds) + " ago"
    import datetime as _dt
    then = _dt.datetime.now() - _dt.timedelta(seconds=ago_seconds)
    clock = then.strftime("%H:%M")
    today = _dt.date.today()
    if then.date() == today:
        return "seen today at " + clock
    if then.date() == today - _dt.timedelta(days=1):
        return "seen yesterday at " + clock
    return "seen " + then.strftime("%Y-%m-%d") + " at " + clock


def humanise(seconds):
    # Compound above the unit boundary: "65m" reads like a typo, "1h 5m" like
    # a clock. The remainder is dropped only when it is zero.
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 172800:
        h, m = seconds // 3600, (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = seconds // 86400, (seconds % 86400) // 3600
    return f"{d}d {h}h" if h else f"{d}d"




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


def _project_entry(cfg, path):
    key = os.path.normcase(os.path.normpath(path or ""))
    for entry in cfg.get("projects", []):
        if os.path.normcase(os.path.normpath(entry.get("path", ""))) == key:
            return entry
    return None


def sync_intent(cfg, path):
    """Did the person leave this project syncing? True, False, or None when
    nothing is recorded - a project the list does not hold, or one last
    touched by a version before 2.2.2.

    The person's word, kept: Start sync, a project shared or joined, or an
    engine found running set it; only Stop sync clears it. A restart of the
    app - or of the whole machine, which ends every engine - goes by it: the
    user's ruling of 2026-10-03, "every project that was syncing carries on",
    and its other half, a project they stopped stays stopped.
    """
    entry = _project_entry(cfg, path)
    if entry is None or "sync" not in entry:
        return None
    return bool(entry["sync"])


def set_sync_intent(cfg, path, on):
    """Record it on the project's entry. True if that changed anything, so
    the caller knows to save."""
    entry = _project_entry(cfg, path)
    if entry is None or entry.get("sync") is bool(on):
        return False
    entry["sync"] = bool(on)
    return True


def turned_off(cfg, path):
    """Did the person turn syncing of this project off? Their recorded word
    (sync_intent) - or, where none is recorded, a stop they asked of its
    engine that it has not taken yet: by a version before 2.2.2, or for a
    project taken off the list together with its syncing.

    A stop rides on a request the engine reads at its next rest, and the
    request outlives the app - so the app can be closed, or updated, before
    the engine has read it. The next start used to take such a stop back:
    the window "reconnected" to the engine still running, and the startup
    sweep replaced an older one by a fresh engine. Found 2026-10-03 while
    writing 2.2.2: Stop sync, then an update straight away, and the project
    was syncing again on the new version (test_engine_resume, section 7)."""
    intent = sync_intent(cfg, path)
    if intent is not None:
        return not intent
    return stop_requested(path)


# The files this app plants in every shared project, tracked in its history -
# so a fresh clone has them too. Their presence is the local sign that a
# folder is one of ours; an 'origin' alone is not (Test-SharedProject in
# sync-core.ps1 has what syncing into somebody's own repository did).
TEAMSYNC_MARKERS = ("push-now.ps1", "TEAM-PROJECT-REFERENCE.md")


def has_teamsync_files(path):
    return bool(path) and any(os.path.isfile(os.path.join(path, m)) for m in TEAMSYNC_MARKERS)


def github_origin(path):
    """owner/name of the folder's 'origin' when it is on GitHub, else ''."""
    url = git(path, "remote", "get-url", "origin")
    if "github.com" not in url:
        return ""
    return url.rstrip("/").removesuffix(".git").split("github.com")[-1].strip(":/")


def teamsync_refs_on(slug):
    """Does that GitHub repository carry this app's own refs? True, False, or
    None when GitHub could not be asked."""
    data = gh_json("api", f"repos/{slug}/git/matching-refs/teamsync/")
    if data is None:
        return None
    return bool(data)


def find_shared_history(path):
    """A project this app already shared - from another machine, or from an
    earlier copy of the folder - that holds this folder's history. Its
    owner/name, or "" when there is none, or no history to compare.

    The first commit is the fingerprint: two copies of one project share it,
    two different projects never do. Every repository this account can see is
    asked whether it holds that commit - a few at a time, because each answer
    is a round trip - and a match counts only if it also carries this app's
    refs: the folder's OWN repository holds the commit too, and it is exactly
    the one this must never mistake for a shared project.
    """
    roots = git(path, "rev-list", "--max-parents=0", "HEAD").split()
    if not roots:
        return ""
    names = gh("api", "user/repos?per_page=100", "--paginate", "--jq", ".[].full_name")
    slugs = [s for s in (names or "").split() if "/" in s]
    if not slugs:
        return ""
    from concurrent.futures import ThreadPoolExecutor

    def holds(slug):
        return gh("api", f"repos/{slug}/commits/{roots[0]}", "--jq", ".sha") is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        hits = [s for s, ok in zip(slugs, pool.map(holds, slugs)) if ok]
    for slug in hits:
        if teamsync_refs_on(slug):
            return slug
    return ""


# Every shared project carries these; init-owner refuses to leave them home.
SHARE_RESERVED = (".gitignore", ".gitattributes", "AGENTS.md", "CLAUDE.md",
                  "TEAM-PROJECT-REFERENCE.md", "push-now.ps1", "who.ps1", "working.ps1")


def exclude_arg(keep):
    """The -Exclude value for init-owner. Each path is written "./path", so a
    name that starts with "-" can never be read by PowerShell as a parameter
    of its own; init-owner takes the "./" off again."""
    return "|".join("./" + k for k in keep)


def share_preview(path, keep):
    """What sharing `path` WOULD upload, measured by init-owner itself, which
    touches nothing in the folder (-Preview). The same code then makes the
    real decision, so what the window shows is what the setup does.

    Returns {"items": [(top-level item, bytes, files)], "machine": [(path,
    bytes, files)] left out by default, "history": (bytes, commits) or None,
    "large": [{item, bytes, files, committed}], "error": text or None}.
    """
    out = {"items": [], "machine": [], "history": None, "large": [], "error": None}
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           resource_path("engine", "init-owner.ps1"), "-Path", path, "-Preview"]
    if keep:
        cmd += ["-Exclude", exclude_arg(keep)]
    try:
        p = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=600)
        text = (p.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        out["error"] = str(e)
        return out
    done = False
    for line in text.splitlines():
        f = line.rstrip("\r").split("\t")
        try:
            if f[0] == "TS-ITEM":
                out["items"].append((f[1], int(f[2]), int(f[3])))
            elif f[0] == "TS-MACHINE":
                out["machine"].append((f[1], int(f[2]), int(f[3])))
            elif f[0] == "TS-HISTORY":
                out["history"] = (int(f[1]), int(f[2]))
            elif f[0] == "TS-LARGE":
                out["large"].append({"item": f[1], "bytes": int(f[2]), "files": int(f[3]),
                                     "committed": f[4] == "1"})
            elif f[0] == "TS-ERROR":
                out["error"] = f[1] if len(f) > 1 else "the measurement failed"
            elif f[0] == "TS-END":
                done = True
        except (IndexError, ValueError):
            continue
    if not done and not out["error"]:
        # Not a finished answer - never read as "nothing to worry about".
        out["error"] = text.strip()[-600:] or "the measurement gave no answer"
    out["items"].sort(key=lambda t: _ordinal(t[0]))
    return out


def share_route(path):
    """Where "share this folder" should lead. The user's rule, 2026-10-01:

    if this app already shared it - recently or long ago, from this machine or
    another - the next person is added to THAT project. Otherwise it does not
    matter whether the folder has git, or a GitHub repository of its own: this
    is not publishing it, it is building a shared workspace, so a NEW private
    repository is made for developing together.

    Returns ("shared", slug) - this very folder is a shared project;
    ("elsewhere", slug) - its history is shared as another project; or
    ("new", ""). Network-bound: call it off the main thread.
    """
    has_git = os.path.isdir(os.path.join(path, ".git"))
    slug = github_origin(path) if has_git else ""
    if slug and (has_teamsync_files(path) or teamsync_refs_on(slug)):
        return "shared", slug
    if has_git:
        hit = find_shared_history(path)
        if hit and hit.lower() != slug.lower():
            return "elsewhere", hit
    return "new", ""

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


def register_editor_extension(base, dst):
    """List the planted extension in the editor's own install registry.

    Modern VS Code and its forks load only extensions recorded in
    extensions/extensions.json; a folder merely sitting in the extensions
    directory is ignored - measured on VS Code 1.124, where the planted
    folder stayed invisible to --list-extensions until this entry existed.
    Old builds scan the folder and never read the entry, so writing both
    serves every version. The editor rewrites the registry when it exits and
    may drop an entry added while it was running; planting runs at every app
    start, so the entry returns until an editor restart finally carries it in.
    """
    reg = os.path.join(base, "extensions", "extensions.json")
    try:
        entries = []
        if os.path.exists(reg):
            with open(reg, encoding="utf-8") as fh:
                entries = json.load(fh)
        if not isinstance(entries, list):
            return
        for e in entries:
            if isinstance(e, dict) and \
                    (e.get("identifier") or {}).get("id") == "teamsync-local.teamsync-presence":
                return
        fs_path = dst[0].lower() + dst[1:]
        posix = "/" + fs_path.replace("\\", "/")
        entries.insert(0, {
            "identifier": {"id": "teamsync-local.teamsync-presence"},
            "version": "1.0.0",
            "location": {"$mid": 1, "fsPath": fs_path, "_sep": 1,
                         "external": "file://" + urllib.parse.quote(posix),
                         "path": posix, "scheme": "file"},
            "relativeLocation": "teamsync.presence-1.0.0",
            "metadata": {"isApplicationScoped": False, "isMachineScoped": False,
                         "isBuiltin": False,
                         "installedTimestamp": int(time.time() * 1000),
                         "pinned": False, "source": "vsix"},
        })
        # Written whole to a side file first: a torn registry would cost the
        # editor every extension it knows, not just ours.
        tmp = reg + ".teamsync-tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, separators=(",", ":"))
        os.replace(tmp, reg)
    except (OSError, ValueError):
        return


def install_editor_extension():
    """Plant the editor-presence extension where VS Code loads extensions from.

    The extension is TeamSync's eyes inside the editor: which project files
    are open, which carry unsaved typing, and the moment they close - facts
    Windows itself keeps no record of. Planting is two acts: the files copied
    into the editor's extensions folder, and the entry in its install
    registry that makes modern builds actually load them. Both are picked up
    at the editor's next restart. With no VS Code on the machine the folder
    simply sits unread; nothing breaks.
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
        if os.path.isdir(dst):
            register_editor_extension(base, dst)


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
        "You pick a folder that already holds your work. That folder and everything under it becomes a PRIVATE repository on your GitHub account - only the people you invite can see it - and syncing starts.\n\n"
        "People you have shared with before are listed for you: click a name, or Ctrl+click several. For somebody new, type their GitHub username. Inviting nobody now is fine; people can be added later.\n\n"
        "Once per project. The repository name must be plain lower-case latin - if the folder name is not, type one yourself."
    ),
    'join': (
        "Join a project someone shared with you.\n\n"
        "It offers two ways in. The first is the requests waiting for you: someone invited you through GitHub, and their request is listed with their name and the repository, to accept or decline.\n\n"
        "The second is by hand, for when no request shows up and you already know the repository name and whose account it is on. It downloads into any empty folder you choose; a short latin path without spaces gives the fewest surprises.\n\n"
        "A repository you were never invited to answers \"repository not found\", which looks like a typo but is not. If you expected a request and see none, this machine may be signed in to a different GitHub account - check with: gh auth status"
    ),
    'addpeople': (
        "Add people to this project.\n\n"
        "A project does not have to be finished being shared on the day it started. This invites more people to the one that is already running - the same tick-box list you saw when you created it.\n\n"
        "They get a GitHub invitation, which appears under \"Requests received\" in their own TeamSync. Accepting downloads the project and starts their syncing.\n\n"
        "Only whoever created the project can invite. If that is not you, the button says so instead of failing after you press it."
    ),
    'conflicts': (
        "Conflicts on this project.\n\n"
        "Everything that is stuck right now, whoever it belongs to. A conflict happens inside one person's folder, and only that machine can finish its merge - but nobody should have to wait in the dark for it.\n\n"
        "For somebody else's conflict, \"Read all versions\" writes out all three: their version, what is on the shared branch, and what both started from. If you decide what the file should say, edit it and press Publish now - their side takes your version when it arrives.\n\n"
        "While anybody is resolving, the app stops publishing your work AUTOMATICALLY, so the ground does not move under them. Publish now and push-now.ps1 still send immediately: that is a decision, and decisions stay yours."
    ),
    'destructive': (
        "Changes waiting for your word.\n\n"
        "Adding work needs no permission. Destroying it does - so when what is about to go out would DELETE a whole file, or put a file back to an older version of itself, the app stops and asks you first. Everything else keeps publishing as usual: editing a file - adding lines, removing lines - never asks.\n\n"
        "Moving or renaming a file does not ask either, as long as its content arrives unchanged under the new name: nothing is destroyed. Moved AND changed, or moved somewhere the project ignores, still asks.\n\n"
        "It happens by accident more often than on purpose: a file dragged to the bin, a folder restored from an old backup, an editor that saved over the newest text. Published, that removes the file or the newer wording from everybody's machine - and it used to go out with the log saying only \"pushed 1 commit(s)\".\n\n"
        "It looks at everything not yet sent - work on the disk, and work already committed on this machine - and at every earlier version of a file, however old.\n\n"
        "Two answers. \"Publish these\" sends them, and they really do disappear for everyone. \"Put them back\" restores each file from the right place - the newest version on this machine, or the team's version if the damage was already committed here. Nothing was ever lost; every version is here.\n\n"
        "The confirmation covers exactly the files listed. Delete something else afterwards and you are asked again, because you have not seen that one yet.\n\n"
        "A machine that is simply behind is never caught by this: it is measured from the point where this machine and the team last agreed, so a file it has not received yet is not a file it deleted. Signing in from a second computer is safe.\n\n"
        "LARGE content waits here too: one file of 25 MB or more, or one new folder adding 50 MB or more. Once uploaded it stays in the project's history for good - deleting it later takes it out of the folder, never out of the history. A new folder counts as one item however many small files it holds, so a freshly installed node_modules is one line, not ten thousand.\n\n"
        "\"Keep them home\" puts the item in .gitignore - which travels, so nobody else sends their copy either - and if part of it was already inside a commit made here and not sent, takes it back out of that commit. Every file stays on your disk exactly as it is. \"Send them\" uploads it: right for real work that is simply big. A file the team already has can only be sent - keeping it home would take it away from everybody.\n\n"
        "Either answer covers that one publish. The same folder grown again next month is asked about again."
    ),
    'requests': (
        "Requests received.\n\n"
        "When somebody invites you to a shared project, GitHub holds that invitation for you, and it is listed here: who invited you, and to which repository.\n\n"
        "Accept downloads the project and starts syncing. Decline tells GitHub no - the request disappears for both of you, nothing is downloaded, and they can invite you again later.\n\n"
        "It sits on the top line beside Help, with the buttons that govern the whole project rather than today's work, and appears only while a project is open - you would otherwise never see an invitation while working. On the first screen the same news shows as a number on the Join button.\n\n"
        "The button is grey while nothing is waiting. It checks about once a minute and lights up on its own, so there is nothing to refresh by hand."
    ),
    'open_existing': (
        "Open a project already on this machine.\n\n"
        "For later days, or for moving between several shared projects. Pick the project folder itself - the one containing .git - not the folder above it.\n\n"
        "A project that is still syncing but is not on the list - removed from it earlier, say - is shown too, marked \"syncing - not on your list\", so you can open it again or stop it.\n\n"
        "Removing a project that is still syncing asks whether its syncing should stop too.\n\n"
        "Opening a project you turned off keeps it off until you press Start sync.\n\n"
        "You rarely need this: the app reopens your last project on its own, and every project that was syncing carries on by itself."
    ),
    'publish': (
        "Publish now.\n\n"
        "Commits what you have and sends it, so the other person has it within seconds.\n\n"
        "Nothing is lost without it: work goes out automatically after 4 quiet minutes. This button is for when a piece is finished and you would rather not wait. AI agents press the same button through push-now.ps1.\n\n"
        "It also clears the warning your teammate sees on the files you were editing."
    ),
    'syncbtn': (
        "Turn syncing on or off.\n\n"
        "Off means nothing is sent and nothing is received until you turn it on again - and it stays off when the app or the whole machine restarts. Your work stays safe in local commits either way.\n\n"
        "Turning it off takes a few seconds, and the button says Stopping... meanwhile: the engine first finishes what it is in the middle of. A git command cut off halfway can leave the project stuck for the next engine. Close or update the app in those seconds and the stop still happens - the next start finishes it, never undoes it.\n\n"
        "Closing the window does NOT turn syncing off - the engine keeps running in the background. This button and Disconnect are the only off switches."
    ),
    'openfolder': (
        "Open the project folder in Windows Explorer."
    ),
    'relocate': (
        "Change folder.\n\n"
        "If you moved or renamed the project folder, point the app at its new location from here.\n\n"
        "No need to close the window or go Home first. Syncing stops at the old path - once its engine has finished what it is in the middle of - and then starts at the new one. Nothing is lost."
    ),
    'home': (
        "Home.\n\n"
        "Back to the first screen, where you can open a different project, start a new one, or join one. Syncing of the current project is NOT stopped - it carries on in the background."
    ),
    'disconnect': (
        "Disconnect this project.\n\n"
        "Stops its sync engine completely - nothing left running in the background - and removes the project from the app's list. The engine first finishes what it is in the middle of, which takes a few seconds.\n\n"
        "Nothing is deleted: the files on disk and the GitHub repository are untouched. Reconnect any time with \"Open a project\"."
    ),
    'partner': (
        "Who is on this project.\n\n"
        "Up to five people online are named outright, each with its own light: green is syncing right now, grey is away with how long ago they were last seen.\n\n"
        "Past five, the names stop being readable at a glance and it shows the count instead. Hover it for up to ten of them, and click it for everybody.\n\n"
        "When more than ten are online, the ten you are shown are the busiest: most work landed in the last three hours, then the last day, then the last week - each window filling only the places the one before could not.\n\n"
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
        "When on, TeamSync opens itself after every login and carries on syncing every project that was syncing - not only the last one. A project you turned off stays off.\n\n"
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


class TeamPanel(ttk.Frame):
    """Who is here, in the corner where one partner light used to be.

    Up to five people online are named outright, one under the next, each
    with its own lamp - at that size a list IS the summary. Past five the
    names stop being readable at a glance and a count is the honest thing to
    show; the names are then a hover away and the whole team a click away.

    The lamp is the plain circle U+25CF, not an emoji: the character takes
    the foreground colour, while an emoji is drawn by the font in its own
    colours and can never be green. (The project chooser needed painted
    images for the same reason - it had tried an emoji.)
    """

    MAX_ROWS = 5          # named outright up to here
    HOVER_MAX = 10        # shown in the hover list

    def __init__(self, parent, on_click):
        super().__init__(parent)
        self.on_click = on_click
        self.rows = []
        self.people = []
        self.activity = {}
        self.summary = ttk.Label(self, text="", font=FONT_B, foreground=FG, cursor="hand2")
        self.summary.bind("<Button-1>", lambda e: self.on_click())
        self.summary.bind("<Enter>", self._hover_in)
        self.summary.bind("<Leave>", self._hover_out)
        self.popup = None
        self._hide_job = None

    # -- drawing ----------------------------------------------------------

    def clear(self, note="checking who is here..."):
        """Between projects: show nothing that belongs to the last one, and
        claim nothing about this one until it has been read. ("nobody has
        joined yet" would be a claim, and for a second it would be false.)"""
        self.people, self.activity = [], {}
        for widget in self.rows:
            widget.destroy()
        self.rows = []
        self.summary.pack_forget()
        self._row(note, MUTED, "")

    def set(self, people, activity):
        """people: [{name, ago, online}] already sorted online-first."""
        self.people = list(people)
        self.activity = dict(activity or {})
        for widget in self.rows:
            widget.destroy()
        self.rows = []
        self.summary.pack_forget()

        online = [p for p in self.people if p["online"]]
        if len(online) > self.MAX_ROWS:
            self.summary.config(text=latin(f"{len(online)} people online"))
            self.summary.pack(anchor="e")
            return

        if online:
            for person in online:
                self._row(person["name"], OK, "online")
        elif self.people:
            # Nobody here now. The most recently seen person, with when -
            # which is the whole answer when a project has one other member,
            # and the case this corner has always served.
            newest = self.people[0]
            self._row(newest["name"], WARN, seen_phrase(newest["ago"]))
            if len(self.people) > 1:
                self._more(f"+{len(self.people) - 1} more")
        else:
            self._row("nobody has joined yet", MUTED, "")

        if online and len(self.people) > len(online):
            self._more(f"+{len(self.people) - len(online)} offline")

    def _row(self, name, colour, note):
        row = ttk.Frame(self)
        row.pack(anchor="e")
        ttk.Label(row, text="●", foreground=colour,
                  font=("Segoe UI", 11)).pack(side="left", padx=(0, 8))
        text = latin(name) + (latin(f"  {note}") if note else "")
        ttk.Label(row, text=text, font=FONT_B, foreground=colour).pack(side="left")
        self.rows.append(row)

    def _more(self, text):
        row = ttk.Label(self, text=latin(text), font=("Segoe UI", 8),
                        foreground=MUTED, cursor="hand2")
        row.pack(anchor="e")
        row.bind("<Button-1>", lambda e: self.on_click())
        row.bind("<Enter>", self._hover_in)
        row.bind("<Leave>", self._hover_out)
        self.rows.append(row)

    # -- who goes in the short list ---------------------------------------

    def shortlist(self):
        """The up-to-ten shown on hover.

        Online first. If more than ten are online, the ten are the busiest -
        by work landed in the last three hours, then the last day, then the
        last week, each window filling only what the one before could not.
        If fewer than ten are online, the rest are the people seen most
        recently, so the list is never padded with strangers before it is
        filled with the people who were just here.
        """
        online = [p for p in self.people if p["online"]]
        offline = [p for p in self.people if not p["online"]]
        if len(online) > self.HOVER_MAX:
            order = rank_by_activity([p["name"] for p in online], self.activity)
            by_name = {p["name"]: p for p in online}
            chosen = [by_name[n] for n in order[:self.HOVER_MAX]]
        else:
            chosen = online + offline[:self.HOVER_MAX - len(online)]
        return chosen[:self.HOVER_MAX]

    # -- the hover list ----------------------------------------------------

    def _hover_in(self, _event=None):
        if self._hide_job:
            self.after_cancel(self._hide_job)
            self._hide_job = None
        if self.popup is not None:
            return
        rows = self.shortlist()
        if not rows:
            return
        pop = tk.Toplevel(self)
        pop.overrideredirect(True)          # no title bar: it is a tooltip
        pop.attributes("-topmost", True)
        frame = ttk.Frame(pop, padding=(12, 8))
        frame.pack(fill="both", expand=True)
        try:
            pop.configure(background=PANEL)
        except tk.TclError:
            pass
        for person in rows:
            line = ttk.Frame(frame)
            line.pack(anchor="w")
            colour = OK if person["online"] else MUTED
            ttk.Label(line, text="●", foreground=colour,
                      font=("Segoe UI", 10)).pack(side="left", padx=(0, 8))
            note = "online" if person["online"] else seen_phrase(person["ago"])
            ttk.Label(line, text=latin(f"{person['name']}  -  {note}"),
                      font=FONT, foreground=FG).pack(side="left")
        hidden = len(self.people) - len(rows)
        if hidden > 0:
            ttk.Label(frame, text=latin(f"+{hidden} more - click for everyone"),
                      font=("Segoe UI", 8), foreground=MUTED).pack(anchor="w", pady=(6, 0))
        # Entering the popup must not count as leaving the label, or moving
        # the mouse onto it would close the thing it is moving onto.
        pop.bind("<Enter>", self._hover_in)
        pop.bind("<Leave>", self._hover_out)
        pop.update_idletasks()
        x = self.winfo_rootx() + self.winfo_width() - pop.winfo_width()
        y = self.winfo_rooty() + self.winfo_height() + 4
        pop.geometry(f"+{max(x, 0)}+{y}")
        self.popup = pop

    def _hover_out(self, _event=None):
        # A short delay, because the pointer briefly belongs to neither
        # widget while it crosses the gap between them.
        if self._hide_job:
            self.after_cancel(self._hide_job)
        self._hide_job = self.after(220, self._hide_popup)

    def _hide_popup(self):
        self._hide_job = None
        if self.popup is not None:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None


class TeamWindow(tk.Toplevel):
    """Everybody on the project, online first, then by when they were last here."""

    def __init__(self, parent, people, activity):
        super().__init__(parent)
        self.title("Who is on this project")
        self.transient(parent)
        self.grab_set()
        self.minsize(420, 260)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        online = [p for p in people if p["online"]]
        head = (f"{len(online)} online, {len(people) - len(online)} away"
                if people else "Nobody has joined yet")
        ttk.Label(wrap, text=head, font=FONT_H, foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text="Everyone who has ever connected to this project.",
                  font=FONT, foreground=MUTED).pack(anchor="w", pady=(4, 12))

        area = ScrollArea(wrap, height=220)
        area.pack(fill="both", expand=True)
        for person in people:
            line = ttk.Frame(area.inner)
            line.pack(fill="x", pady=2)
            colour = OK if person["online"] else MUTED
            ttk.Label(line, text="●", foreground=colour,
                      font=("Segoe UI", 11)).pack(side="left", padx=(0, 10))
            ttk.Label(line, text=latin(person["name"]), font=FONT_B,
                      foreground=FG).pack(side="left")
            note = "online" if person["online"] else seen_phrase(person["ago"])
            ttk.Label(line, text=latin(note), font=("Segoe UI", 9),
                      foreground=colour).pack(side="right")
            counts = activity.get(person["name"])
            if counts and counts[2]:
                ttk.Label(line, text=latin(f"{counts[2]} this week"),
                          font=("Segoe UI", 8), foreground=MUTED).pack(side="right", padx=(0, 14))

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(14, 0))
        button(row, "Close", self._close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 70
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _close(self):
        self.grab_release()
        self.destroy()


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


# ----------------------------------------------------- people and invitations ---
#
# Everything in this section answers one question the app used to ask the
# person instead: who are you, who do you work with, and who has asked you to
# join something. All three answers already exist - in the GitHub account the
# machine is signed in to, in the repositories that account can see, and in
# git's own settings on this disk. Asking a human to retype them was the
# friction; reading them is the fix.
#
# The rule for the whole section: GitHub is the record, this is a cache of it.
# A hand-kept list of teammates cannot know that somebody was removed from a
# repository on github.com, and would go on offering them for years.


def gh_json(*args):
    """Run a gh command and parse its JSON output.

    Parsed here rather than with `--jq` so that a failed call is None and an
    empty result is [] - two different things that --jq would flatten into one
    empty string, which reads as "nobody" when it means "could not ask".
    """
    out = gh(*args)
    if out is None:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def git_identity(cfg):
    """The name and email git already commits under on this machine.

    Looked for in a project this person already shares before falling back to
    the global setting, because the project copy is what they actually chose
    for shared work. Returns (name, email), either possibly empty.
    """
    seen = []
    for entry in [{"path": cfg.get("last_project")}] + list(cfg.get("projects", [])):
        path = entry.get("path") if isinstance(entry, dict) else None
        if not path or path in seen:
            continue
        seen.append(path)
        if os.path.isdir(os.path.join(path, ".git")):
            name = git(path, "config", "user.name")
            mail = git(path, "config", "user.email")
            if name or mail:
                return name, mail
    return (git(None, "config", "--global", "user.name"),
            git(None, "config", "--global", "user.email"))


def detect_identity(cfg):
    """Fill in who this person is, so no form ever asks them twice.

    GitHub is asked only for the username - the one fact only it knows. The
    name and email come from git on this machine. GitHub's own email address
    is deliberately NOT requested: reading it needs a wider token scope than
    this app has any business holding, and the answer is already on the disk.
    When the account has never committed anywhere, the address falls back to
    GitHub's own noreply form, which is always valid and never leaks a real
    inbox.

    Returns the identity dict and writes it into cfg. Network-bound - call it
    off the main thread.
    """
    login = gh("api", "user", "--jq", ".login") or cfg.get("my_login", "")
    name, mail = git_identity(cfg)
    ident = {
        "my_login": login,
        "me": cfg.get("me") or name or login,
        "email": cfg.get("email") or mail or (f"{login}@users.noreply.github.com" if login else ""),
    }
    cfg.update({k: v for k, v in ident.items() if v})
    return ident


def _repo_people(slug, me):
    """Everyone attached to one repository, as (login, pending) pairs.

    Three kinds of person show up here: the owner of a repository somebody
    shared with ME, the collaborators on one I own, and the people I have
    invited who have not accepted yet. The last group matters - they are the
    ones most likely to be invited again by mistake.
    """
    found = []
    owner = slug.split("/")[0]
    if owner and owner.lower() != (me or "").lower():
        found.append((owner, False))
    people = gh_json("api", f"repos/{slug}/collaborators", "--paginate")
    for who in people or []:
        login = (who or {}).get("login", "")
        if login and login.lower() != (me or "").lower():
            found.append((login, False))
    # Only an admin may read this one; on a repository we merely joined it
    # answers 403, which gh_json turns into None and this loop skips.
    invites = gh_json("api", f"repos/{slug}/invitations")
    for inv in invites or []:
        if (inv or {}).get("expired"):
            continue                  # dead: nobody is waiting on it any more
        login = ((inv or {}).get("invitee") or {}).get("login", "")
        if login:
            found.append((login, True))
    return found


def _hidden_set(cfg):
    return {s.lower() for s in cfg.get("people_hidden", []) if s}


def refresh_people(cfg):
    """Keep the history of everyone this person has ever shared a project with.

    The list is a HISTORY, not a census. Sharing with somebody once puts them
    in it and they stay: the question this list answers is "who might I share
    with next", and a project that ended does not make a person a stranger.
    So GitHub is used to DISCOVER people - it finds the teammate on a
    repository the app never set up - but it is not the authority on who
    stays. Nothing here removes anybody.

    The one way out is the person saying so, which puts a name in
    `people_hidden` and is honoured here for good. Network-bound - call it off
    the main thread.
    """
    me = cfg.get("my_login", "")
    hidden = _hidden_set(cfg)
    by_login = {}
    for entry in cfg.get("people", []):
        login = entry.get("login", "")
        if login and login.lower() not in hidden:
            by_login[login.lower()] = dict(entry)

    for entry in cfg.get("projects", []):
        slug = repo_slug(entry.get("path", ""))
        if not slug:
            continue
        for login, pending in _repo_people(slug, me):
            if login.lower() in hidden:
                continue
            rec = by_login.setdefault(login.lower(), {"login": login, "name": ""})
            rec["login"] = login
            rec["pending"] = pending
            shared = rec.setdefault("projects", [])
            short = slug.split("/")[-1]
            if short not in shared:
                shared.append(short)

    people = sorted(by_login.values(), key=lambda r: r.get("login", "").lower())
    cfg["people"] = people
    return people


def remember_person(cfg, login, name=""):
    """Record somebody the moment they are invited, before GitHub is asked again.

    Without this the roster would stay empty until the next refresh, so the
    very person just invited would be missing from the list on the next
    screen - the one place they are most likely to be wanted.

    Inviting somebody who was removed from the list puts them back. Choosing
    them again is a plainer statement of intent than the removal was.
    """
    login = (login or "").strip()
    if not login:
        return
    cfg["people_hidden"] = [s for s in cfg.get("people_hidden", [])
                            if s.lower() != login.lower()]
    for rec in cfg.setdefault("people", []):
        if rec.get("login", "").lower() == login.lower():
            if name and not rec.get("name"):
                rec["name"] = name
            return
    cfg["people"].append({"login": login, "name": name, "projects": [], "pending": True})


def forget_person(cfg, login):
    """Take somebody out of the list of people to offer next time.

    Local only, and worth being exact about: it does not remove them from any
    repository, does not end any sharing, and does not tell them anything. It
    only stops this app suggesting them. Remembered as a name to skip rather
    than by deletion, because the next refresh would otherwise rediscover
    them from GitHub and put them straight back.
    """
    login = (login or "").strip()
    if not login:
        return
    cfg["people"] = [r for r in cfg.get("people", [])
                     if r.get("login", "").lower() != login.lower()]
    hidden = cfg.setdefault("people_hidden", [])
    if login.lower() not in {s.lower() for s in hidden}:
        hidden.append(login)


# How many conditional asks may go by before one full, unconditional read.
# A 304 is free, which is why the poll uses one - but it also means the app
# can only ever be as right as the last answer that was NOT a 304. Measured on
# round 7: the button sat at its startup count for over THIRTY MINUTES while
# GitHub's own API listed a new invitation, and opening the window - which
# reads afresh - showed it at once. Whatever made that conditional answer go
# stale, a cache with no floor can never recover from it on its own. Six full
# reads an hour against a budget of five thousand is not a cost.
INVITE_FULL_EVERY = 5

_invite_etag = {"etag": None, "value": [], "conditional": 0}


def _invitations_raw(force=False):
    """The raw invitation list, asked for as cheaply as the answer allows.

    Asked again every minute so the button un-greys on its own, so the cost of
    asking matters. A conditional request answers 304 when nothing has
    changed, which is fast and does not count against the rate limit at all -
    the same trick the release check uses. When there is no HTTP client (no
    token readable), it falls back to gh, which is correct but not free.
    """
    opener = _release_client()
    if opener is None:
        return gh_json("api", "/user/repository_invitations")
    import urllib.error
    import urllib.request
    req = urllib.request.Request("https://api.github.com/user/repository_invitations")
    if force or _invite_etag["conditional"] >= INVITE_FULL_EVERY:
        _invite_etag["etag"] = None            # ask outright, and believe it
        _invite_etag["conditional"] = 0
    if _invite_etag["etag"]:
        req.add_header("If-None-Match", _invite_etag["etag"])
    try:
        with opener.open(req, timeout=15) as res:
            _invite_etag["etag"] = res.headers.get("ETag")
            _invite_etag["value"] = json.loads(res.read().decode("utf-8", "replace"))
            _invite_etag["conditional"] = 0
            return _invite_etag["value"]
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            _invite_etag["conditional"] += 1
            return _invite_etag["value"]        # unchanged since last look
        return None
    except Exception:
        return None


def pending_invitations(force=False):
    """Invitations waiting for this account on GitHub.

    This is what closes the app's oldest piece of friction: joining used to
    require going to github.com, finding the invitation and accepting it by
    hand, because init-friend fails with "repository not found" against a
    repository the account cannot see yet. The invitation was always readable
    through the API; nothing was ever shown.
    """
    data = _invitations_raw(force=force)
    if data is None:
        # Could not ask. That is NOT the same as "nobody has invited you", and
        # returning an empty list here made a waiting invitation vanish off the
        # screen on any passing network hiccup. The caller keeps what it had.
        return None
    out = []
    expired = {}
    for inv in data:
        repo = (inv or {}).get("repository") or {}
        full = repo.get("full_name", "")
        if not full or "/" not in full:
            continue
        owner, name = full.split("/", 1)
        item = {
            "id": inv.get("id"),
            "owner": owner,
            "name": name,
            "full": full,
            "inviter": ((inv.get("inviter") or {}).get("login") or owner),
            "private": bool(repo.get("private")),
            "created": (inv.get("created_at") or "")[:10],
        }
        # GitHub keeps listing an invitation after it expires (seven days),
        # marked "expired" - and accepting one is a trap: measured on
        # 2026-10-01, GitHub answered the accept with success, removed the
        # invitation, and added nobody, so the download that followed was
        # told "repository not found". Never offered; cleared by the caller.
        if inv.get("expired"):
            expired[item["id"]] = item
            continue
        out.append(item)
    _invite_expired.clear()
    _invite_expired.update(expired)
    return out


_invite_expired = {}     # id -> invitation, seen expired in GitHub's last answer


def clear_expired_invitations():
    """Remove, for good, every expired invitation GitHub's last answer carried.

    Declining is the only door the invited side has, and for a dead invitation
    it costs nothing: it could never be accepted. Returns the ones removed, so
    the person can be told who once invited them - the inviter can always send
    a fresh one.
    """
    removed = []
    for inv_id, inv in list(_invite_expired.items()):
        if decline_invitation(inv_id):
            removed.append(inv)
        _invite_expired.pop(inv_id, None)
    return removed


def sweep_sent_invitations():
    """The other side of the same rule: delete the invitations THIS account
    sent that have expired, on every repository it owns. Returns
    [(owner/name, invitee, sent-on)] for the ones removed.

    An expired invitation is dead on GitHub already; what it still does is sit
    in the invited person's list looking alive (2.1.9 offered it with an
    Accept button). Removing it at the source clears it from their screen
    whatever version of the app they run.
    """
    names = gh("api", "user/repos?affiliation=owner&per_page=100", "--paginate",
               "--jq", ".[].full_name")
    removed = []
    for slug in (names or "").split():
        for inv in gh_json("api", f"repos/{slug}/invitations") or []:
            if not (inv or {}).get("expired"):
                continue
            gone = gh("api", "-X", "DELETE", f"repos/{slug}/invitations/{inv.get('id')}")
            if gone is not None:
                removed.append((slug, ((inv.get("invitee") or {}).get("login") or "?"),
                                (inv.get("created_at") or "")[:10]))
    return removed


def repo_visible(full, tries=3, wait=2.0):
    """Can this account see that repository now? Asked right after accepting
    an invitation, because GitHub's "accepted" was measured to mean nothing
    when the invitation had expired. A couple of retries cover the moment it
    takes access to settle; a definite no after them is the answer."""
    for attempt in range(tries):
        if gh_json("api", f"repos/{full}") is not None:
            return True
        if attempt + 1 < tries:
            time.sleep(wait)
    return False


def _answer_invitation(invitation_id, method):
    """Accept (PATCH) or decline (DELETE) one invitation. True if GitHub took it.

    Both answers are 204 No Content, so success arrives as an EMPTY string,
    not a missing one - the check must be `is not None`, or every success
    would be read as a failure. The cached list is dropped either way: the
    invitation just answered is gone, and a 304 on the next look would
    otherwise keep serving it.
    """
    ok = gh("api", "-X", method, f"/user/repository_invitations/{invitation_id}") is not None
    if ok:
        _invite_etag["etag"] = None
        _invite_etag["value"] = []
    return ok


def accept_invitation(invitation_id):
    """Take the invitation: this account becomes a collaborator."""
    return _answer_invitation(invitation_id, "PATCH")


def decline_invitation(invitation_id):
    """Refuse the invitation. It disappears for both sides; the person who
    sent it can send another. Nothing is downloaded and no access is gained."""
    return _answer_invitation(invitation_id, "DELETE")


# ------------------------------------------------------------- setup forms ---

class ScrollArea(ttk.Frame):
    """A frame whose contents scroll when there are more than fit.

    Put rows inside `.inner`. The scrollbar appears only when it is needed,
    because a permanent one beside three names is a claim that something is
    hidden when nothing is.
    """

    def __init__(self, parent, height):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0,
                                background=BG, borderwidth=0)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        # The inner frame decides the scrollable height; the canvas decides the
        # width. Without the second binding the rows keep their natural width
        # and every label is cut off at the first long username.
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._window, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_scroll(self, first, last):
        # Shown only while something is out of sight.
        if float(first) <= 0.0 and float(last) >= 1.0:
            self.bar.pack_forget()
        else:
            self.bar.pack(side="right", fill="y")
        self.bar.set(first, last)

    def _wheel(self, event):
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")


class PeoplePicker(ttk.Frame):
    """Choose who to share with: the people already worked with, or a new name.

    Built as a list of tick boxes rather than a box to type in, because the
    name needed is a GitHub username - exact, easy to mistype, and already
    known to the app for everybody this person has shared with before.

    A real Checkbutton rather than a selected row or a drawn square: only the
    real widget shows at a glance which names are ticked when the list is
    longer than the eye, keeps its tick while the mouse goes elsewhere, and
    answers to the keyboard. The same reason the buttons in this app are
    themed widgets and not canvas drawings.

    Any number can be ticked. Nothing downstream depends on there being
    exactly one: the invitation loop takes as many as it is given, which is
    the first piece of the app that no longer assumes two people.
    """

    def __init__(self, parent, cfg, on_change=None):
        super().__init__(parent)
        self.cfg = cfg
        self.on_change = on_change
        self.ticks = {}

        self.head = ttk.Label(self, text="People you have shared with before",
                              font=FONT_B, foreground=FG)
        self.hint = ttk.Label(self, text="Tick everyone this project is for.",
                              font=("Segoe UI", 8), foreground=MUTED)
        self.area = ScrollArea(self, height=150)

        self.typed_label = ttk.Label(self, text="", font=FONT_B, foreground=FG)
        self.typed = tk.StringVar(value="")
        self.entry = ttk.Entry(self, textvariable=self.typed, font=FONT)
        self.foot = ttk.Label(self, text="Separate several with a comma. "
                                         "Leave empty to invite nobody for now.",
                              font=("Segoe UI", 8), foreground=MUTED)
        self._render()

    def _render(self):
        """Draw the list from the config, so a removal can simply redraw."""
        for widget in (self.head, self.hint, self.area, self.typed_label,
                       self.entry, self.foot):
            widget.pack_forget()
        for child in self.area.inner.winfo_children():
            child.destroy()

        people = [r for r in self.cfg.get("people", []) if r.get("login")]
        # A ticked box must survive a redraw: removing one person is not a
        # reason to un-tick everybody else.
        keep = {login: var.get() for login, var in self.ticks.items()}
        self.ticks = {}

        if people:
            self.head.pack(anchor="w", pady=(8, 2))
            self.hint.pack(anchor="w", pady=(0, 6))
            self.area.configure(height=min(len(people), 5) * 30)
            self.area.canvas.configure(height=min(len(people), 5) * 30)
            self.area.pack(fill="x")
            for rec in people:
                self._row(rec, keep)

        self.typed_label.config(text="Or a GitHub username" if people
                                else "Their GitHub username")
        self.typed_label.pack(anchor="w", pady=(10, 2))
        self.entry.pack(anchor="w", fill="x", ipady=6)
        self.foot.pack(anchor="w")

    def _row(self, rec, keep):
        login = rec["login"]
        var = tk.BooleanVar(value=keep.get(login, False))
        self.ticks[login] = var
        row = ttk.Frame(self.area.inner)
        row.pack(fill="x", pady=1)
        name = rec.get("name") or login
        # The username is the part that must be right, so it is shown even
        # when a friendlier name exists - never instead of it.
        caption = name if name == login else f"{name}  ({login})"
        ttk.Checkbutton(row, text=caption, variable=var).pack(side="left")
        ttk.Button(row, text="x", width=2, style="Small.TButton",
                   command=lambda l=login: self._forget(l)).pack(side="right", padx=(8, 2))
        where = ", ".join(rec.get("projects", []) or [])
        if rec.get("pending"):
            where = (where + " - invitation not accepted yet").strip(" -")
        if where:
            ttk.Label(row, text=where, font=("Segoe UI", 8),
                      foreground=MUTED).pack(side="right", padx=(8, 4))

    def _forget(self, login):
        if not messagebox.askyesno(
                APP_NAME,
                "Remove " + login + " from the list of people to offer next time?" + NN +
                "This only changes this list on this machine. It does not remove "
                "them from any project, does not stop any sharing that is already "
                "running, and tells them nothing." + NN +
                "Inviting them again puts them back.",
                parent=self.winfo_toplevel()):
            return
        forget_person(self.cfg, login)
        save_config(self.cfg)
        self._render()
        if self.on_change:
            self.on_change()

    def chosen(self):
        """Every username ticked or typed, de-duplicated, order kept."""
        names = [login for login, var in self.ticks.items() if var.get()]
        for chunk in self.typed.get().replace(";", ",").split(","):
            chunk = chunk.strip().lstrip("@")
            if chunk:
                names.append(chunk)
        out = []
        for n in names:
            if n.lower() not in [o.lower() for o in out]:
                out.append(n)
        return out


class ExclusionWindow(tk.Toplevel):
    """Choose what stays on this machine before a new project is shared.

    The user's design (2026-10-01): asked at sharing time, with the answer
    built up in as many rounds as the person likes - one or several files or
    folders at a time - and a final confirmation at the end. Everything the
    window shows comes from init-owner's own measurement, re-taken after every
    change, so the sizes and the large warnings are the ones the setup itself
    will act on.

    result: None (back out), or {"keep": [paths], "allow_large": bool}.
    """

    def __init__(self, parent, app, folder, preview, keep=None):
        super().__init__(parent)
        self.app = app
        self.folder = os.path.normpath(folder)
        self.preview = preview
        self.keep = list(keep or [])
        self.result = None
        self.busy = False
        self.title("Choose what stays on this machine")
        self.transient(parent)
        self.grab_set()
        self.minsize(720, 560)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="What goes to the shared project", font=FONT_H,
                  foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text="Everything under \"Shared\" is uploaded for the people you invite. "
                             "Keep anything that is\nyours alone on this machine - as many times "
                             "as you like, then press Share.",
                  font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 10))

        ttk.Label(wrap, text="Shared", font=FONT_B, foreground=FG).pack(anchor="w")
        self.shared_area = ScrollArea(wrap, height=170)
        self.shared_area.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Stays on this machine", font=FONT_B,
                  foreground=FG).pack(anchor="w", pady=(10, 0))
        self.kept_area = ScrollArea(wrap, height=90)
        self.kept_area.pack(fill="both", expand=False)

        self.machine_lbl = ttk.Label(wrap, text="", font=FONT, foreground=MUTED,
                                     justify="left", wraplength=660)
        self.machine_lbl.pack(anchor="w", pady=(8, 0))
        self.history_lbl = ttk.Label(wrap, text="", font=FONT, foreground=MUTED,
                                     justify="left", wraplength=660)
        self.history_lbl.pack(anchor="w")
        self.status_lbl = ttk.Label(wrap, text="", font=FONT_B, foreground=FG)
        self.status_lbl.pack(anchor="w", pady=(8, 0))

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(12, 0))
        self.btn_files = button(row, "Keep files home...", self._add_files)
        self.btn_files.pack(side="left")
        self.btn_folder = button(row, "Keep a folder home...", self._add_folder)
        self.btn_folder.pack(side="left", padx=(8, 0))
        self.btn_share = button(row, "Share", self._share, primary=True)
        self.btn_share.pack(side="right")
        button(row, "Back", self._back).pack(side="right", padx=(0, 8))
        self.protocol("WM_DELETE_WINDOW", self._back)
        self._render()

    # --- what is shown ---------------------------------------------------------
    def _large_names(self):
        return {h["item"] for h in self.preview.get("large", [])}

    def _render(self):
        for area in (self.shared_area, self.kept_area):
            for c in area.inner.winfo_children():
                c.destroy()
        big = self._large_names()
        items = self.preview.get("items", [])
        for name, size, files in items:
            line = ttk.Frame(self.shared_area.inner)
            line.pack(fill="x", anchor="w")
            colour = "#ffd7d7" if name in big else FG
            tag = "   LARGE" if name in big else ""
            ttk.Label(line, text=latin("%s   %s, %d file(s)%s" % (name, size_text(size), files, tag)),
                      font=MONO, foreground=colour).pack(side="left")
            if name not in SHARE_RESERVED:
                b = button(line, "Keep home", lambda n=name: self._add([n]))
                b.pack(side="right")
                if self.busy:
                    b.state(["disabled"])
        if not items:
            ttk.Label(self.shared_area.inner, text="nothing - every file stays on this machine",
                      font=FONT, foreground=MUTED).pack(anchor="w")
        for k in self.keep:
            line = ttk.Frame(self.kept_area.inner)
            line.pack(fill="x", anchor="w")
            ttk.Label(line, text=latin(k), font=MONO, foreground=FG).pack(side="left")
            b = button(line, "Share it after all", lambda k=k: self._remove(k))
            b.pack(side="right")
            if self.busy:
                b.state(["disabled"])
        if not self.keep:
            ttk.Label(self.kept_area.inner, text="nothing yet", font=FONT,
                      foreground=MUTED).pack(anchor="w")

        machine = self.preview.get("machine", [])
        if machine:
            total = sum(m[1] for m in machine)
            names = ", ".join(m[0] for m in machine[:6]) + (" ..." if len(machine) > 6 else "")
            self.machine_lbl.configure(text=latin(
                "Left out automatically (%s) - tools rebuild these on every machine: %s"
                % (size_text(total), names)))
        else:
            self.machine_lbl.configure(text="")
        hist = self.preview.get("history")
        in_history = [h["item"] for h in self.preview.get("large", []) if h.get("committed")]
        text = ""
        if hist:
            text = ("This folder's own git history goes up as it is: %s in %d commit(s)."
                    % (size_text(hist[0]), hist[1]))
        if in_history:
            text += ("\nLarge, and already in that history (it goes up with it): "
                     + ", ".join(in_history))
        self.history_lbl.configure(text=latin(text))

        if self.busy:
            self.status_lbl.configure(text="measuring...", foreground=MUTED)
        elif self.preview.get("error"):
            self.status_lbl.configure(text=latin("Could not measure: " + self.preview["error"][:200]),
                                      foreground=BAD)
        else:
            size = sum(i[1] for i in items)
            files = sum(i[2] for i in items)
            self.status_lbl.configure(
                text="Shared: %d file(s), %s%s" % (files, size_text(size),
                                                    "  -  large content included" if big else ""),
                foreground="#ffd7d7" if big else FG)
        for b in (self.btn_files, self.btn_folder, self.btn_share):
            b.state(["disabled"] if self.busy else ["!disabled"])
        if self.preview.get("error") and not self.busy:
            self.btn_share.state(["disabled"])

    # --- changing it -----------------------------------------------------------
    def _relative(self, picked):
        """picked as a path inside the folder, with "/" - or None."""
        full = os.path.normpath(picked)
        try:
            if os.path.commonpath([os.path.normcase(self.folder), os.path.normcase(full)]) \
                    != os.path.normcase(self.folder):
                return None
        except ValueError:          # another drive
            return None
        rel = os.path.relpath(full, self.folder).replace(os.sep, "/")
        return None if rel in (".", "") or rel.startswith("../") else rel

    def _add(self, names):
        refused = [n for n in names if n.rstrip("/") in SHARE_RESERVED]
        if refused:
            messagebox.showinfo(
                "Stays shared",
                ", ".join(refused) + " is part of every shared project - the others need it.",
                parent=self)
        added = False
        for n in names:
            if n.rstrip("/") in SHARE_RESERVED or n in self.keep:
                continue
            self.keep.append(n)
            added = True
        if added:
            self._remeasure()

    def _add_files(self):
        picked = filedialog.askopenfilenames(parent=self, initialdir=self.folder,
                                             title="Files that stay on this machine")
        names, outside = [], []
        for f in picked or ():
            rel = self._relative(f)
            (names.append(rel) if rel else outside.append(f))
        self._outside(outside)
        if names:
            self._add(names)

    def _add_folder(self):
        picked = filedialog.askdirectory(parent=self, initialdir=self.folder, mustexist=True,
                                         title="A folder that stays on this machine")
        if not picked:
            return
        rel = self._relative(picked)
        if not rel:
            self._outside([picked])
            return
        self._add([rel.rstrip("/") + "/"])

    def _outside(self, paths):
        if paths:
            messagebox.showinfo("Not in this project",
                                "Only what is inside the project folder can be kept home:\n\n"
                                + "\n".join(paths), parent=self)

    def _remove(self, k):
        if k in self.keep:
            self.keep.remove(k)
            self._remeasure()

    def _remeasure(self):
        self.busy = True
        self._render()
        keep, folder = list(self.keep), self.folder

        def work():
            pv = share_preview(folder, keep)
            self.app.post(lambda: self._measured(keep, pv))
        threading.Thread(target=work, daemon=True).start()

    def _measured(self, keep, pv):
        if not self.winfo_exists():
            return
        if keep != self.keep:        # changed again meanwhile: the next answer is coming
            return
        self.busy = False
        self.preview = pv
        self._render()

    # --- the final word ----------------------------------------------------------
    def _share(self):
        if self.busy or self.preview.get("error"):
            return
        items = self.preview.get("items", [])
        size = sum(i[1] for i in items)
        files = sum(i[2] for i in items)
        large = self.preview.get("large", [])
        text = ("Share %s now?\n\nShared: %d file(s), %s."
                % (os.path.basename(self.folder), files, size_text(size)))
        if self.keep:
            text += "\nStays on this machine: " + ", ".join(self.keep[:12]) + \
                    (" ..." if len(self.keep) > 12 else "")
        if large:
            text += ("\n\nLARGE content goes up with it, and stays in the project's history "
                     "for good:\n" + "\n".join("  %s  %s" % (h["item"], size_text(h["bytes"]))
                                                 for h in large[:10]))
        if not messagebox.askyesno("Share it?", text, parent=self):
            return
        self.result = {"keep": list(self.keep), "allow_large": bool(large)}
        self.destroy()

    def _back(self):
        self.result = None
        self.destroy()


class SetupDialog(tk.Toplevel):
    """Collects what init-owner.ps1 / init-friend.ps1 need.

    In join mode it can be handed an invitation, in which case the two facts
    that used to be typed from a chat message - whose account, which
    repository - are filled in and shown as settled, and the only question
    left is where to put the folder.
    """

    def __init__(self, parent, mode, cfg, invite=None, path=None):
        super().__init__(parent)
        self.mode = mode
        self.invite = invite
        self.preset_path = path or ""
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

        if mode == "owner":
            note = ("The folder, including sub-folders, is uploaded to a new PRIVATE\n"
                    "repository on your GitHub account. Only the people you invite\n"
                    "here can see it. You are asked first whether anything should\n"
                    "stay on this machine; its .gitignore, and what tools rebuild\n"
                    "(node_modules, Python caches), stay home anyway.\n\n"
                    "A folder with its own git history keeps it, and any GitHub\n"
                    "repository it already had is left alone. A folder this app has\n"
                    "shared before is not shared twice - the people are added to it.")
        elif invite:
            note = (f"{invite['inviter']} invited you to {invite['full']}.\n"
                    "Choosing a folder accepts the invitation and downloads the project.")
        else:
            note = ("Accept the GitHub invitation first, otherwise this fails with\n"
                    "\"repository not found\" - which is not a typo on your side.")
        ttk.Label(wrap, text=note, font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 14))

        self.vars = {}
        self.picker = None
        if mode == "owner":
            self._folder_row(wrap, "path", "Project folder", pick_existing=True)
            self._row(wrap, "reponame", "Repository name", "",
                      "Lower-case latin only. Leave empty to use the folder name.")
            self.picker = PeoplePicker(wrap, cfg)
            self.picker.pack(fill="x")
        elif invite:
            self._fixed_row(wrap, "reponame", "Repository", invite["name"])
            self._fixed_row(wrap, "owner", "Shared by", invite["owner"])
            self._folder_row(wrap, "path", "Download into (any empty folder you like)",
                             pick_existing=False)
        else:
            self._row(wrap, "reponame", "Repository name", "", "The name they gave you.")
            self._row(wrap, "owner", "Their GitHub username", cfg.get("owner", ""))
            self._folder_row(wrap, "path", "Download into (any empty folder you like)",
                             pick_existing=False)

        # Identity is detected once and reused. It is still shown, because a
        # person is entitled to see what will be written into their commits,
        # and still editable, because a detected answer can be the wrong one.
        self._row(wrap, "me", "Your name", cfg.get("me", ""), "Shown as the author of your commits.")
        self._row(wrap, "email", "Your GitHub email", cfg.get("email", ""))

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(16, 0))
        button(row, "Cancel", self.destroy).pack(side="right")
        ok = "Start" if mode == "owner" else ("Accept and download" if invite else "Connect")
        button(row, ok, self._ok, primary=True).pack(side="right", padx=(0, 8))

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 70
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _fixed_row(self, parent, key, label, value):
        """A fact that came from GitHub, shown but not up for editing.

        An entry the person could change would invite them to change it, and
        any change would point the download at a repository the invitation
        does not cover.
        """
        ttk.Label(parent, text=label, font=FONT_B, foreground=FG).pack(anchor="w", pady=(8, 2))
        ttk.Label(parent, text=value, font=FONT, foreground=ACCENT).pack(anchor="w")
        self.vars[key] = tk.StringVar(value=value)

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
        var = tk.StringVar(value=self.preset_path if pick_existing else "")
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
        if self.picker:
            vals["people"] = self.picker.chosen()
            # The engine takes one comma-separated string, never a list: with
            # powershell.exe -File a list would bind its second name to the
            # next parameter instead. Measured, not assumed.
            vals["friend"] = ",".join(vals["people"])
        self.result = vals
        self.destroy()


class RequestsWindow(tk.Toplevel):
    """The invitations waiting for this account, each answerable here.

    Accept and Decline sit on the same row on purpose: an invitation is a
    question, and a window that offers only "yes" makes the person go to
    github.com to say no - which is the errand this whole screen exists to
    abolish.

    The list refreshes itself while the window is open, so an invitation that
    arrives, or one answered on the other machine, does not leave a stale row
    to press.
    """

    def __init__(self, parent, app, on_join):
        super().__init__(parent)
        self.app = app
        self.on_join = on_join
        self.title("Requests received")
        self.transient(parent)
        self.grab_set()
        self.minsize(560, 240)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="People asking you to join their project",
                  font=FONT_H, foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text="Accepting downloads the project. Declining tells GitHub no; "
                             "they can invite you again later.",
                  font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 12))

        self.area = ScrollArea(wrap, height=190)
        self.area.pack(fill="both", expand=True)
        self.empty = ttk.Label(wrap, text="", font=FONT, foreground=MUTED)

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(14, 0))
        button(row, "Close", self._close).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.app.watch_invitations(self.render)
        self.render(self.app.invitations())

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 60
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _close(self):
        self.app.unwatch_invitations(self.render)
        self.grab_release()
        self.destroy()

    def render(self, invites):
        for child in self.area.inner.winfo_children():
            child.destroy()
        if not invites:
            self.empty.config(text="Nothing is waiting. This window updates by itself.")
            self.empty.pack(anchor="w", pady=(10, 0))
            return
        self.empty.pack_forget()
        for inv in invites:
            row = ttk.Frame(self.area.inner, padding=(0, 6))
            row.pack(fill="x")
            text = ttk.Frame(row)
            text.pack(side="left", fill="x", expand=True)
            ttk.Label(text, text=inv["full"], font=FONT_B, foreground=FG).pack(anchor="w")
            ttk.Label(text, text=f"invited by {inv['inviter']}"
                                 + ("  -  private" if inv.get("private") else ""),
                      font=("Segoe UI", 8), foreground=MUTED).pack(anchor="w")
            button(row, "Decline", lambda i=inv: self._decline(i), danger=True).pack(side="right")
            button(row, "Accept", lambda i=inv: self._accept(i),
                   primary=True).pack(side="right", padx=(0, 8))

    def _accept(self, inv):
        # The window closes first: accepting opens the folder-choosing dialog,
        # and this one holds a grab that would keep that dialog unusable.
        self._close()
        self.on_join(inv)

    def _decline(self, inv):
        if not messagebox.askyesno(
                APP_NAME,
                "Decline the invitation to " + inv["full"] + "?" + NN +
                inv["inviter"] + " will be able to invite you again, but this "
                "request disappears for both of you.", parent=self):
            return
        if decline_invitation(inv["id"]):
            self.app.lines.put("declined the invitation to " + inv["full"])
            self.app.drop_invitation(inv["id"])
        else:
            messagebox.showwarning(
                APP_NAME,
                "GitHub would not take that. The invitation may already have "
                "been withdrawn, or answered on another machine.", parent=self)
            self.app.refresh_invitations()


class JoinChooser(tk.Toplevel):
    """The two ways in: answer a request, or type the details by hand.

    The second exists because the first depends on GitHub answering and on
    both people being signed in to the accounts they think they are. When
    that goes wrong the person must still be able to join, so the manual door
    is kept and named as what it is.
    """

    def __init__(self, parent, app, on_requests, on_manual):
        super().__init__(parent)
        self.app = app
        self.title("Join a project")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        wrap = ttk.Frame(self, padding=(22, 18))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Join a project someone shared with you",
                  font=FONT_H, foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text="If they have invited you, their request is waiting below.",
                  font=FONT, foreground=MUTED).pack(anchor="w", pady=(4, 16))

        self.req_btn = button(wrap, "Requests received", lambda: self._go(on_requests),
                              primary=True)
        self.req_btn.pack(anchor="w", fill="x")
        self.req_hint = ttk.Label(wrap, text="", font=("Segoe UI", 8), foreground=MUTED)
        self.req_hint.pack(anchor="w", pady=(2, 14))

        button(wrap, "Enter the details by hand", lambda: self._go(on_manual)).pack(anchor="w", fill="x")
        ttk.Label(wrap, text="For when no request shows up and you know the repository name.",
                  font=("Segoe UI", 8), foreground=MUTED).pack(anchor="w", pady=(2, 0))

        ttk.Frame(wrap, height=8).pack()
        button(wrap, "Cancel", self._close).pack(anchor="e")

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.app.watch_invitations(self.refresh)
        self.refresh(self.app.invitations())

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 80
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def refresh(self, invites):
        set_request_button(self.req_btn, invites)
        self.req_hint.config(
            text="Nothing waiting yet - this updates by itself." if not invites
            else "Accept or decline each one.")

    def _close(self):
        self.app.unwatch_invitations(self.refresh)
        self.grab_release()
        self.destroy()

    def _go(self, action):
        self._close()
        action()


def set_request_button(btn, invites):
    """One rule for every 'Requests received' button, wherever it stands.

    Greyed while nothing is waiting, because a button that opens an empty
    list teaches the person to stop pressing it. The count is in the label so
    the answer is known before the window opens.
    """
    count = len(invites or [])
    if count:
        btn.config(text=f"Requests received ({count})")
        btn.state(["!disabled"])
    else:
        btn.config(text="Requests received")
        btn.state(["disabled"])


JOIN_LABEL = "Join a project someone shared with me"


def set_join_button(btn, invites):
    """The count on the join button, which never greys.

    Unlike the requests button, this one always works: the manual way in
    lives behind it, and that is exactly what somebody needs on the day no
    invitation shows up. So the number appears and disappears; the button
    does not.
    """
    count = len(invites or [])
    btn.config(text=f"{JOIN_LABEL} ({count})" if count else JOIN_LABEL)


def repo_admin(slug):
    """Can this account invite people to that repository?

    Asked before the button is offered rather than after it is pressed: a
    button that always fails for half the team is worse than no button.
    Returns True, False, or None when GitHub could not be asked at all.
    """
    data = gh_json("api", f"repos/{slug}")
    if data is None:
        return None
    return bool((data.get("permissions") or {}).get("admin"))


def invite_to_repo(slug, login):
    """Invite one person to an existing project. (ok, message)."""
    out = gh("api", "-X", "PUT", f"repos/{slug}/collaborators/{login}",
             "-f", "permission=push")
    if out is None:
        return False, f"GitHub would not add {login}. Check the username."
    return True, f"invited {login}"


class AddPeopleWindow(tk.Toplevel):
    """Invite more people to a project that is already running.

    The app could only ever invite at creation time, so growing a team meant
    going to github.com and knowing which settings page to find. The roster
    and its tick boxes are the same ones the share form uses - one way to
    choose a person, wherever you are choosing them.
    """

    def __init__(self, parent, app_window, repo=None):
        super().__init__(parent)
        self.app = app_window
        # Bound to ONE project, named on the window, and never to "whichever
        # project happened to be open last". From the project screen that is
        # the open one; from the welcome screen the person picks it first, so
        # the window can still only ever act on the project it names.
        self.repo = repo or app_window.repo
        self.slug = repo_slug(self.repo)
        self.title("Add people to this project")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        wrap = ttk.Frame(self, padding=(22, 18))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Add people to this project", font=FONT_H,
                  foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text=latin(self.slug or "(no GitHub remote)"),
                  font=FONT, foreground=ACCENT).pack(anchor="w", pady=(2, 2))
        ttk.Label(wrap, text=latin(os.path.basename(self.repo.rstrip("\\/")) if self.repo else ""),
                  font=("Segoe UI", 8), foreground=MUTED).pack(anchor="w", pady=(0, 10))

        self.note = ttk.Label(wrap, text="", font=FONT, foreground=MUTED, justify="left")
        self.note.pack(anchor="w", pady=(0, 8))

        self.picker = PeoplePicker(wrap, app_window.cfg)
        self.picker.pack(fill="x")

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(16, 0))
        button(row, "Close", self._close).pack(side="right")
        self.btn = button(row, "Invite", self._invite, primary=True)
        self.btn.pack(side="right", padx=(0, 8))
        self.btn.state(["disabled"])

        self.protocol("WM_DELETE_WINDOW", self._close)
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 60
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        # Asking GitHub takes a moment; do it off the main thread and let the
        # window appear first.
        threading.Thread(target=self._check_rights, daemon=True).start()

    def _check_rights(self):
        allowed = repo_admin(self.slug) if self.slug else False
        self.app.post(lambda: self._rights_known(allowed))

    def _rights_known(self, allowed):
        try:
            if allowed:
                self.note.config(
                    text="They get a GitHub invitation, and it appears under\n"
                         "\"Requests received\" in their own TeamSync.",
                    foreground=MUTED)
                self.btn.state(["!disabled"])
            elif allowed is None:
                self.note.config(
                    text="GitHub could not be reached, so it is not certain you may\n"
                         "invite people here. Check the connection and reopen this.",
                    foreground=WARN)
            else:
                self.note.config(
                    text="Only the person who created this project can invite others.\n"
                         "Ask them to add anybody new.",
                    foreground=WARN)
        except tk.TclError:
            pass                      # the window was closed while we asked

    def _close(self):
        self.grab_release()
        self.destroy()

    def _invite(self):
        chosen = self.picker.chosen()
        if not chosen:
            messagebox.showinfo(APP_NAME, "Tick somebody, or type a GitHub username.",
                                parent=self)
            return
        if not messagebox.askyesno(
                APP_NAME,
                "Invite these people to " + self.slug + "?" + NN +
                "\n".join("  " + c for c in chosen) + NN +
                "They will be able to read and change everything in this project.",
                parent=self):
            return
        self.btn.state(["disabled"])
        threading.Thread(target=lambda: self._send(chosen), daemon=True).start()

    def _send(self, chosen):
        done, failed = [], []
        for login in chosen:
            ok, message = invite_to_repo(self.slug, login)
            (done if ok else failed).append(login)
            self.app.lines.put(message)
            if ok:
                remember_person(self.app.cfg, login)
        self.app.post(lambda: self._sent(done, failed))

    def _sent(self, done, failed):
        save_config(self.app.cfg)
        if done:
            self.app.lines.put(
                "invited: " + ", ".join(done) +
                " - it shows under Requests received in their TeamSync")
        if failed:
            messagebox.showwarning(
                APP_NAME,
                "GitHub would not add:" + NN + "\n".join("  " + f for f in failed) + NN +
                "Usually a mistyped username. The others went out.", parent=self)
        try:
            self.btn.state(["!disabled"])
            if done and not failed:
                self._close()
        except tk.TclError:
            pass


class LargeWindow(tk.Toplevel):
    """Large content about to go out, and the two answers to it.

    Once sent it stays in the project's history for good - deleting it later
    removes it from the folder, never from the history every clone carries.
    Most of what is that big (installed libraries, caches, build output) can
    be rebuilt on each machine, so keeping it home is the usual answer; the
    other is real too, for a big file that IS part of the work.

    A file the team already has cannot be kept home - that would take it out
    of the project for everybody - so it is shown apart, and only sending
    (or putting the old version back by hand) answers it.
    """

    def __init__(self, parent, app, large):
        super().__init__(parent)
        self.app = app
        self.large = list(large)
        self.new = [h for h in self.large if not h.get("shared")]
        self.title("Large content waiting for your word")
        self.transient(parent)
        self.grab_set()
        self.minsize(660, 320)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="This would upload large content",
                  font=FONT_H, foreground="#ffd7d7").pack(anchor="w")
        ttk.Label(wrap, text="Publishing is paused until you say. Once sent, it stays in the "
                             "project's history for good.\nInstalled libraries, caches and build "
                             "output can be rebuilt on each machine - keep those home.",
                  font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 12))

        area = ScrollArea(wrap, height=190)
        area.pack(fill="both", expand=True)
        body = area.inner

        def section(title, items, note):
            if not items:
                return
            ttk.Label(body, text=title, font=FONT_B, foreground=FG).pack(anchor="w", pady=(8, 0))
            ttk.Label(body, text=note, font=FONT, foreground=MUTED,
                      justify="left").pack(anchor="w", pady=(0, 4))
            for h in items:
                where = "   (inside a commit made here)" if h.get("committed") else ""
                ttk.Label(body, text=latin("   %s   %s, %d file(s)%s"
                                           % (h["item"], size_text(h["bytes"]), h["files"], where)),
                          font=MONO, foreground="#ffd7d7").pack(anchor="w")

        section("New to the project", self.new,
                "Nobody else has these yet.")
        section("A new version of a file the team already has",
                [h for h in self.large if h.get("shared")],
                "Every new version is added to the history in full. Keeping it home is not "
                "possible - it is part of the project.")

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(14, 0))
        keep = button(row, "Keep the new ones home" if 0 < len(self.new) < len(self.large)
                      else "Keep them home", self._keep, primary=True)
        keep.pack(side="left")
        if not self.new:
            keep.state(["disabled"])
        button(row, "Send them", self._send, danger=True).pack(side="left", padx=(8, 0))
        button(row, "Decide later", self.destroy).pack(side="right")

    def _keep(self):
        if any(h.get("committed") for h in self.new):
            if not messagebox.askyesno(
                    "Keep them home?",
                    "Some of this is already inside a commit made on this machine and not "
                    "sent yet - and a commit would carry it along.\n\n"
                    "Those unsent commits will be folded into the next one. Every file stays "
                    "exactly as it is on your disk; nothing of yours is lost.", parent=self):
                return
        try:
            keep_large_home(self.app.repo, self.new)
        except GuardError as e:
            messagebox.showerror("Could not keep them home", str(e), parent=self)
            return
        self.app.say("kept home: " + ", ".join(h["item"] for h in self.new)
                     + " - added to the project's .gitignore, which travels to everyone")
        self.app.refresh_destructive()
        self.destroy()

    def _send(self):
        if not messagebox.askyesno(
                "Send them?",
                "Upload " + ", ".join(h["item"] for h in self.large) + "?\n\n"
                "It goes to everyone, and stays in the project's history for good.",
                parent=self):
            return
        approve_large(self.app.repo, self.large)
        self.app.say("confirmed - large content will publish: "
                     + ", ".join(h["item"] for h in self.large))
        self.app.refresh_destructive()
        self.destroy()


def size_text(n):
    """Bytes as a person reads them."""
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / (1024 * 1024))
    return "%d KB" % max(1, n // 1024)


class DestructiveWindow(tk.Toplevel):
    """What is about to destroy work, and the two answers to it.

    The app publishes on its own, which is the whole point of it - but a
    deletion and a reversion travel exactly like an edit, and were measured
    removing a file from every teammate's machine while the log said only
    "pushed 1 commit(s)". So the machine no longer makes that call alone.

    Both answers are real. "Publish these" sends them and they are gone for
    everyone. "Put them back" restores the newest version - the door to the
    history that has always been in every clone and that nobody could reach
    without knowing git.
    """

    def __init__(self, parent, app, changes):
        super().__init__(parent)
        self.app = app
        self.changes = changes
        self.title("Changes waiting for your word")
        self.transient(parent)
        self.grab_set()
        self.minsize(640, 320)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="This would destroy work on everybody's machine",
                  font=FONT_H, foreground="#ffd7d7").pack(anchor="w")
        ttk.Label(wrap, text="Publishing is paused until you say. Everything else keeps syncing "
                             "as usual.\nNothing has been lost either way - every version is "
                             "still in the project's history.",
                  font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 12))

        area = ScrollArea(wrap, height=200)
        area.pack(fill="both", expand=True)
        body = area.inner

        def section(title, files, note):
            if not files:
                return
            ttk.Label(body, text=title, font=FONT_B, foreground=FG).pack(anchor="w", pady=(8, 0))
            ttk.Label(body, text=note, font=FONT, foreground=MUTED,
                      justify="left").pack(anchor="w", pady=(0, 4))
            for f in files:
                ttk.Label(body, text="   " + f, font=MONO,
                          foreground="#ffd7d7").pack(anchor="w")

        section("Would be deleted for everyone", changes.get("deleted", []),
                "These files are in the project but no longer on this disk.")
        section("Would be put back to an older version", changes.get("reverted", []),
                "The text on this disk is one this file already had before - "
                "publishing it replaces newer wording with older.")

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(14, 0))
        button(row, "Put them back", self._restore, primary=True).pack(side="left")
        button(row, "Publish these", self._approve, danger=True).pack(side="left", padx=(8, 0))
        button(row, "Decide later", self.destroy).pack(side="right")

    def _restore(self):
        n = len(self.changes.get("deleted", [])) + len(self.changes.get("reverted", []))
        if not messagebox.askyesno(
                "Put them back?",
                f"Restore {n} file(s) to the newest version the project has?\n\n"
                "Anything you typed into them since is replaced.", parent=self):
            return
        restore_destructive(self.app.repo, self.changes)
        self.app.say("restored " + str(n) + " file(s) - publishing is free again")
        self.app.refresh_destructive()
        self.destroy()

    def _approve(self):
        n = len(self.changes.get("deleted", [])) + len(self.changes.get("reverted", []))
        if not messagebox.askyesno(
                "Publish these?",
                f"Send {n} change(s) that delete or roll back work?\n\n"
                "They will disappear from every teammate's machine too.", parent=self):
            return
        approve_destructive(self.app.repo, self.changes)
        self.app.say("confirmed - " + str(n) + " destructive change(s) will publish")
        self.app.refresh_destructive()
        self.destroy()


class ConflictReportsWindow(tk.Toplevel):
    """Every conflict on the team right now, readable by anybody.

    The point is the second half of the owner's rule: a conflict belongs to
    one machine, but nobody should have to wait in the dark for it. Anyone
    can read what is stuck, see all three versions, and - if they want -
    write the final text and publish it. The stuck side takes that version
    when it arrives.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.title("Conflicts on this project")
        self.transient(parent)
        self.grab_set()
        self.minsize(640, 300)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)
        ttk.Label(wrap, text="Conflicts being resolved right now",
                  font=FONT_H, foreground=FG).pack(anchor="w")
        ttk.Label(wrap, text="A conflict happens on one machine, and only that machine can finish "
                             "its merge.\nYou can still read every version, and publish the final "
                             "text yourself if you get there first.",
                  font=FONT, foreground=MUTED, justify="left").pack(anchor="w", pady=(4, 12))

        self.area = ScrollArea(wrap, height=200)
        self.area.pack(fill="both", expand=True)
        self.empty = ttk.Label(wrap, text="", font=FONT, foreground=MUTED)

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=(14, 0))
        button(row, "Close", self._close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.render()

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 60
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _close(self):
        self.grab_release()
        self.destroy()

    def render(self):
        for child in self.area.inner.winfo_children():
            child.destroy()
        repo = self.app.repo
        rows, me, claimed = [], "", {}
        if repo:
            me = presence_name(repo)
            git(repo, "fetch", "-q", "--prune", "origin",
                "+refs/teamsync/volunteer/*:refs/teamsync/volunteer/*")
            claimed = conflict_volunteers(repo)
            for who, files in team_conflicts(repo, me).items():
                for path in files:
                    rows.append((who, path, False))
            # Our own, named as ours, so one window answers "what is stuck"
            # rather than only "what is stuck for other people".
            for path in (git(repo, "diff", "--name-only", "--diff-filter=U") or "").splitlines():
                if path.strip():
                    rows.append((me, path.strip(), True))
        if not rows:
            self.empty.config(text="Nothing is in conflict. This is the quiet answer.")
            self.empty.pack(anchor="w", pady=(10, 0))
            return
        self.empty.pack_forget()
        for who, path, is_mine in rows:
            line = ttk.Frame(self.area.inner, padding=(0, 6))
            line.pack(fill="x")

            volunteer = claimed.get((who, path), "")
            # The stuck person can always work on their own conflict - it is
            # their rebase, and nobody else can finish it for them. Beyond
            # that, exactly one other person may claim it: two people settling
            # the same file separately is not help, it is the next conflict.
            mine_to_take = is_mine or not volunteer or volunteer == me
            if not is_mine:
                var = tk.BooleanVar(value=(volunteer == me))
                box = ttk.Checkbutton(
                    line, text="", variable=var,
                    command=lambda w=who, p=path, v=var: self._claim(w, p, v))
                box.pack(side="left", padx=(0, 8))
                if volunteer and volunteer != me:
                    box.state(["disabled"])
            else:
                ttk.Label(line, text="   ").pack(side="left", padx=(0, 8))

            text = ttk.Frame(line)
            text.pack(side="left", fill="x", expand=True)
            ttk.Label(text, text=latin(path), font=FONT_B, foreground=FG).pack(anchor="w")
            if is_mine:
                note, colour = "yours to finish - nobody else can", WARN
            elif volunteer == me:
                note, colour = f"stuck for {who} - you volunteered to settle it", OK
            elif volunteer:
                note, colour = f"stuck for {who} - {volunteer} is settling it", MUTED
            else:
                note, colour = f"stuck for {who} - tick the box to volunteer", MUTED
            ttk.Label(text, text=latin(note), font=("Segoe UI", 8),
                      foreground=colour).pack(anchor="w")

            if is_mine:
                button(line, "Open my copies", self.app._open_conflict).pack(side="right")
            elif mine_to_take:
                button(line, "Read all versions",
                       lambda w=who, p=path: self._open_report(w, p)).pack(side="right")

    def _claim(self, who, path, var):
        """Take the job, or hand it back."""
        repo = self.app.repo
        me = presence_name(repo)
        wanted = bool(var.get())
        if not set_volunteer(repo, who, path, me, wanted):
            var.set(not wanted)
            messagebox.showwarning(
                APP_NAME,
                "That did not reach GitHub." + NN +
                "Check the connection and try again - until it lands, nobody "
                "else can see that you have taken it.", parent=self)
            return
        self.app.lines.put(
            (f"you volunteered to settle {who}'s conflict in {path}" if wanted
             else f"you handed {who}'s conflict in {path} back"))
        self.render()

    def _open_report(self, who, path):
        repo = self.app.repo
        folder = os.path.join(repo, "_conflicts", f"{who}-report")
        try:
            written = write_conflict_report(repo, who, path, folder)
        except OSError as exc:
            messagebox.showwarning(APP_NAME, f"Could not write the report: {exc}", parent=self)
            return
        if all(v is None for k, v in conflict_report(repo, who, path).items()):
            messagebox.showinfo(
                APP_NAME,
                "Their side has not reached GitHub yet." + NN +
                "The engine publishes it within about fifteen seconds of a conflict "
                "starting. Try again in a moment.", parent=self)
            return
        self.app.lines.put(f"wrote {who}'s conflict report to _conflicts/{who}-report")
        try:
            os.startfile(folder)                      # noqa: S606 - a folder, by design
        except OSError:
            messagebox.showinfo(APP_NAME, "The report is in:" + NN + folder, parent=self)


def project_status(path):
    """One project's state at a glance, for the chooser's per-row light.

    The same signals the main window's pill uses, read from that project's
    own repository - so every row in the list answers for itself, not for
    whichever project happens to be on screen. Returns (text, tag).
    """
    if not os.path.isdir(os.path.join(path, ".git")):
        return ("folder is missing", "missing")
    g = os.path.join(path, ".git")
    rebasing = (os.path.isdir(os.path.join(g, "rebase-merge"))
                or os.path.isdir(os.path.join(g, "rebase-apply")))
    if rebasing or git(path, "diff", "--name-only", "--diff-filter=U"):
        return ("conflict - needs attention", "bad")
    dirty = bool(git(path, "status", "--porcelain"))
    ahead = git(path, "rev-list", "--count", "origin/main..HEAD") or "0"
    engine = ENGINES.running(path)       # a project mid-restart is not "off"
    if dirty or ahead != "0":
        what = (ahead + " unpublished") if ahead != "0" else "unsaved changes"
        return (what + (", syncing" if engine else ", engine off"), "warn")
    if engine:
        return ("all published, syncing", "ok")
    return ("all published, engine off", "idle")


class ProjectChooser(tk.Toplevel):
    """Pick from what has been opened before, instead of hunting for a folder.

    Built on a Treeview rather than hand-made rows: selection, keyboard movement
    and double-click come with the widget. The first attempt drew its own rows
    and tracked selection by repainting their background - which cannot work on
    themed widgets, because they have no background to set, so clicking a row did
    nothing at all.
    """

    def __init__(self, parent, projects, running=()):
        super().__init__(parent)
        self.result = None
        self.projects = list(projects)
        # Engines running for folders that are NOT on the list - taken off it
        # while their engine ran on. They used to sync out of sight, with no
        # way back to them but Browse: a test project did that for a day.
        self.running = list(running)
        self.title("Open a project")
        self.transient(parent)
        self.grab_set()
        self.minsize(620, 300)

        wrap = ttk.Frame(self, padding=(20, 16))
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Projects on this machine", font=FONT_H).pack(anchor="w")
        hint = ("Double-click one, or select it and press Open."
                if (self.projects or self.running)
                else "Nothing has been opened on this machine yet.")
        ttk.Label(wrap, text=hint, foreground=MUTED).pack(anchor="w", pady=(2, 12))

        holder = ttk.Frame(wrap)
        holder.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(holder, columns=("status", "path"), show="tree headings",
                                 selectmode="browse", height=8)
        self.tree.heading("#0", text="Project")
        self.tree.heading("status", text="State")
        self.tree.heading("path", text="Folder")
        self.tree.column("#0", width=190, stretch=False)
        self.tree.column("status", width=185, stretch=False)
        self.tree.column("path", width=330)
        bar = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.tag_configure("missing", foreground=BAD)
        self.tree.tag_configure("bad", foreground=BAD)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("idle", foreground=MUTED)

        # Tk renders emoji glyphs in monochrome, so a text lamp can never be
        # green - these are painted pixel by pixel instead, like the pills at
        # the top of the main window. Kept on self: Tk shows an image only for
        # as long as a Python reference keeps it alive.
        def lamp(color, size=11):
            img = tk.PhotoImage(width=size, height=size)
            r = size // 2
            for yy in range(size):
                for xx in range(size):
                    if (xx - r) ** 2 + (yy - r) ** 2 <= r * r:
                        img.put(color, (xx, yy))
            return img
        self._lamps = {"ok": lamp(OK), "warn": lamp(WARN), "bad": lamp(BAD),
                       "idle": lamp(MUTED), "missing": lamp("#5a3a3a")}

        for entry in self.projects:
            path = entry.get("path", "")
            name = entry.get("name") or os.path.basename(path.rstrip(chr(92) + "/")) or path
            state, tag = project_status(path)
            self.tree.insert("", "end", text=" " + name, image=self._lamps[tag],
                             values=(latin(state), path), tags=(tag,))
        for path in self.running:
            name = os.path.basename(path.rstrip(chr(92) + "/")) or path
            self.tree.insert("", "end", text=" " + name, image=self._lamps["warn"],
                             values=(latin("syncing - not on your list"), path),
                             tags=("warn",))
        if self.tree.get_children():
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
        # values = (status, path) since the state column arrived; the path is
        # the second value - returning the first would hand back "all
        # published" as a folder name.
        return self.tree.item(sel[0], "values")[1]

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
        ENGINES.opened()            # engines are kept per project there, not here
        self.lines = queue.Queue()
        # Work handed back from background threads. `after` is NOT safe to
        # call from another thread - it registers a Tcl command on the
        # interpreter - and it fails outright when the main loop is not
        # turning. A queue drained on the main thread is the same trick this
        # app already uses for log lines, and it cannot fail that way.
        self._jobs = queue.Queue()
        self.conflict_dir = None
        self._log_pos = 0
        self._update_ready = None      # path of a downloaded newer exe
        self._update_available = None  # {tag,size,date} seen but not taken
        self._update_busy = False
        self._invitations = []         # what GitHub last said is waiting
        self._invite_watchers = []     # windows to tell when that changes
        self._team_people = []         # everyone on this project, freshest first
        self._team_activity = {}       # {name: (3h, 24h, 7d)} - only when needed
        # The project is READ on worker threads and only painted here. One
        # look at a time each: a look still out is never stacked on.
        self._poll_busy = False
        self._partner_snap = None      # the last finished look at the team
        self._destructive = {"deleted": [], "reverted": []}
        self._large = []
        self._destructive_busy = False
        self._destructive_again = False
        self._setting_up = False       # a share or a join is writing the folder
        self._painted_repo = None      # whose team and holds are on screen
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
        # Who am I, who do I work with, who has invited me - asked once at
        # startup, off the main thread. Started late enough that the window is
        # already on screen: the answers only decorate it.
        self.after(900, lambda: threading.Thread(target=self._probe_account, daemon=True).start())
        self.after(60000, self._invite_tick)

        remembered = self.repo
        if (remembered and os.path.isdir(os.path.join(remembered, ".git"))
                and has_teamsync_files(remembered)):
            self._show_project()
            self.start_sync()
        elif remembered and os.path.isdir(remembered):
            # Remembered, present, but not a shared project: a share that did
            # not finish left it here (2.1.9 remembered a folder before its
            # setup had succeeded). Opening it would only start an engine that
            # has nothing to sync with - so start on the first screen, and say
            # why, instead of reopening it on every start.
            self.repo = ""
            self.cfg.pop("last_project", None)
            save_config(self.cfg)
            self._show_welcome()
            self._show_note(
                f"The last folder opened here was never shared:\n{remembered}\n"
                "So it is not opened. To work on it together, share it.",
                "never_shared", remembered)
        else:
            self.repo = ""
            self._show_welcome()
            if remembered:
                # A moved or renamed folder is the usual cause, and silently
                # showing the welcome screen makes it look like the project was
                # lost. Nothing is lost - the repository travelled with the folder.
                self._show_note(
                    f"The last project is not at this path any more:\n{remembered}\nIf you moved the folder, use \"Open a project already on this machine\" to point at its new location. Nothing has been lost.",
                    "moved")

        # After the project above has had its own engine seen to, so the two
        # never reach for the same one (ENGINES.claim settles it either way).
        if SWEEP_ENGINES_AT_START:
            self.after(1000, self._sweep_engines)

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
        self.help_btn = ttk.Button(self.topbar, text="Help", width=8,
                                   command=self._show_help)
        self.help_btn.pack(side="left")
        # Home, right beside Help: the way back to the first screen. It used
        # to be "Switch project", down among the estate buttons - but what it
        # does is go home, and the user's own words were that it should say
        # so and sit next to Help. Shown only while a project is open, like
        # everything else on this line that acts on one: on the first screen
        # you are already home.
        self.home_row = ttk.Frame(self.topbar)
        button(self.home_row, "Home", self._switch).pack(side="left")
        help_button(self.home_row, "home").pack(side="left", padx=(3, 0))
        # Beside Help on the top line, not down among the working buttons:
        # an invitation is news about the estate, not an action on today's
        # file. Like the estate strip it appears only while a project is
        # open - on the welcome screen the same news rides as a number on the
        # Join button, where it is already the reason to press Join.
        self.req_row = ttk.Frame(self.topbar)
        self.req_btn = button(self.req_row, "Requests received", self._open_requests)
        self.req_btn.pack(side="left")
        help_button(self.req_row, "requests").pack(side="left", padx=(3, 0))
        set_request_button(self.req_btn, [])
        # The estate-management strip: buttons that govern the whole project
        # rather than the work of the moment. They share the top line with
        # Help, and appear only while a project is open - the top bar itself
        # is permanent, the welcome screen has nothing to manage.
        self.mgmt_row = ttk.Frame(self.topbar)
        self.btn_sync = button(self.mgmt_row, "Stop sync", self.toggle_sync)
        self.btn_sync.pack(side="left")
        help_button(self.mgmt_row, "syncbtn").pack(side="left", padx=(3, 10))
        button(self.mgmt_row, "Add people", self._add_people).pack(side="left")
        help_button(self.mgmt_row, "addpeople").pack(side="left", padx=(3, 10))
        button(self.mgmt_row, "Disconnect", self._disconnect, danger=True).pack(side="left")
        help_button(self.mgmt_row, "disconnect").pack(side="left", padx=(3, 0))

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
        self.status_row = status_row = ttk.Frame(right)
        status_row.pack(anchor="e")
        self.pill = Pill(status_row)
        self.pill.pack(side="left")
        # A spacer the same width as a help button, so the two lights line up
        # even though only the lower one has something to explain.
        ttk.Label(status_row, text="  ", width=3).pack(side="left", padx=(8, 0))

        # Who is on THIS project - so it is shown only while a project is open
        # (_show_project / _show_welcome). On the first screen there is no team,
        # the panel is empty, and its "?" used to sit alone under the status
        # light, which made the whole corner look out of line - the user's
        # words: "it is crooked, idle is not in its place". 2.1.9 had it too,
        # unseen, because the app always opened on a project.
        self.partner_row = partner_row = ttk.Frame(right)
        self.team = TeamPanel(partner_row, self._show_team)
        self.team.pack(side="left")
        help_button(partner_row, "partner").pack(side="left")

        # "Who is holding what" used to live here, as one amber line running
        # left from the corner. It is standing STATE, not an event, and a
        # horizontal run-on line is the one shape that cannot hold it: with two
        # people it fitted, with ten it would push the whole header about. It
        # now has its own column beside the log, where it grows downwards.

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
        # One note, two kinds of news, each with its own button - see _show_note.
        self.missing_locate = button(self.missing, "Locate the folder", self._locate_missing,
                                     danger=True)
        self.missing_share = button(self.missing, "Share this folder", self._share_never_shared,
                                    primary=True)
        self._never_shared_path = None

        self.welcome_actions = ttk.Frame(self.welcome)
        self.welcome_actions.pack(anchor="w", fill="x")

        for text, cmd, key, primary in (
            ("Start a new shared project", lambda: self._setup("owner"), "start_new", True),
            ("Join a project someone shared with me", self._join, "join", False),
            ("Open a project already on this machine", self._open_existing, "open_existing", False),
            ("Add people to a project", self._add_people, "addpeople", False),
        ):
            row = ttk.Frame(self.welcome_actions)
            row.pack(anchor="w", pady=4)
            btn = button(row, text, cmd, primary=primary)
            btn.pack(side="left")
            help_button(row, key).pack(side="left", padx=(6, 0))
            if key == "join":
                # The count rides on the join button itself rather than on a
                # button of its own: on this screen an invitation is a reason
                # to press Join, not a separate errand.
                self.join_btn = btn

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
        action(bar, "Open folder", self._open_folder, "openfolder")
        action(bar, "Change folder", self._relocate, "relocate")
        action(bar, "History", self._show_history, "history")
        self.btn_conflicts = action(bar, "Conflicts", self._show_conflicts, "conflicts")
        # Grey while nothing is held. It is the only place in the app that can
        # stop work leaving the machine, so it says so in red when it lights.
        self.btn_destructive = action(bar, "Needs your OK", self._show_destructive,
                                      "destructive", danger=True)
        self.btn_destructive.state(["disabled"])

        self.banner = ttk.Frame(self.project, padding=(18, 12))
        self.banner_lbl = ttk.Label(self.banner, text="", font=FONT_B,
                                   foreground="#ffd7d7", justify="left")
        self.banner_lbl.pack(side="left")
        button(self.banner, "Open both versions", self._open_conflict, danger=True).pack(side="right")

        split = ttk.Frame(self.project, padding=(18, 0))
        split.pack(fill="both", expand=True, pady=(0, 16))

        # Two columns, because there are two different KINDS of thing here and
        # they were sharing one shape. The log is a stream: events, in order,
        # each true once. "Right now" is state: who holds which file, who is
        # stuck, what is waiting for a word - each true until it is not, and
        # each replaced rather than appended. State in a stream scrolls away;
        # state on one header line runs off the edge as soon as the team grows.
        # A column does neither: it is replaced in place and grows downwards.
        self.nowcol = ttk.Frame(split, width=300)
        self.nowcol.pack(side="right", fill="y", padx=(16, 0))
        self.nowcol.pack_propagate(False)
        ttk.Label(self.nowcol, text="Right now", font=FONT_B,
                  foreground=MUTED).pack(anchor="w", pady=(0, 6))
        self.nowarea = ScrollArea(self.nowcol, height=200)
        self.nowarea.pack(fill="both", expand=True)
        self._now_signature = None

        logwrap = ttk.Frame(split)
        logwrap.pack(side="left", fill="both", expand=True)
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

    # -- people, identity and invitations ----------------------------------

    def _probe_account(self):
        """Ask GitHub who this person is, who they work with, and who invited them.

        One background pass rather than three, because all three answers come
        from the same signed-in account and the same round trips. Off the main
        thread: every call here can hang on a slow network, and the window
        must stay alive. Nothing here writes to a widget - the results are
        handed back through `after`, per the rule that Tk belongs to the main
        thread.
        """
        try:
            detect_identity(self.cfg)
            refresh_people(self.cfg)
            invites = pending_invitations()
        except Exception:
            return
        self.post(lambda: self._account_ready(invites))
        # Expired invitations, both ways: the ones waiting for THIS person,
        # and the ones this person sent that nobody took up in time.
        self._report_cleared(clear_expired_invitations(), [])
        try:
            sent = sweep_sent_invitations()
        except Exception:
            sent = []
        self._report_cleared([], sent)

    def _report_cleared(self, received, sent):
        """Say what the expired-invitation sweep removed. Safe from any thread."""
        for inv in received:
            self.lines.put(f"removed an expired invitation to {inv['full']} from "
                           f"{inv['inviter']}"
                           + (f" (sent {inv['created']})" if inv.get("created") else "")
                           + " - it could no longer be accepted; ask them for a new one")
        for slug, who, when in sent:
            self.lines.put(f"removed your expired invitation for {who} to {slug}"
                           + (f" (sent {when})" if when else "")
                           + " - nobody accepted it in time; Add people sends a new one")

    def _account_ready(self, invites):
        """Main-thread half of the probe: save what was learned and show it."""
        save_config(self.cfg)
        self._set_invitations(invites)

    # -- the invitation list, and everything watching it -------------------

    def invitations(self):
        return list(self._invitations)

    def watch_invitations(self, callback):
        """Every window showing invitations asks to be told when they change.

        Kept as a list of callbacks rather than each window polling on its own
        timer: one question to GitHub, one answer, every screen consistent.
        """
        if callback not in self._invite_watchers:
            self._invite_watchers.append(callback)

    def unwatch_invitations(self, callback):
        if callback in self._invite_watchers:
            self._invite_watchers.remove(callback)

    def _set_invitations(self, invites):
        if invites is None:
            return          # could not ask - keep showing what we last knew
        self._invitations = list(invites)
        # The two faces of the same news: a number on the join button of the
        # first screen, and the button inside a project - so it is seen from
        # wherever the person happens to be sitting.
        set_join_button(self.join_btn, self._invitations)
        set_request_button(self.req_btn, self._invitations)
        # A copy, because a watcher may close its window and unregister
        # itself while this loop is running.
        for callback in list(self._invite_watchers):
            try:
                callback(self._invitations)
            except tk.TclError:
                self.unwatch_invitations(callback)   # its window is gone

    def drop_invitation(self, invitation_id):
        """Take one answered invitation off every screen, at once.

        An invitation that has just been accepted or declined is gone, and
        the count must say so in the same breath as the press - waiting for
        the next round trip would leave a number that is visibly wrong for a
        second, on the one screen whose whole job is to be trusted. GitHub is
        asked again straight afterwards, so this is a head start on the
        truth, never a substitute for it.

        Safe from any thread.
        """
        def apply():
            self._set_invitations([i for i in self._invitations
                                   if i.get("id") != invitation_id])
            self.refresh_invitations()
        self.post(apply)

    def refresh_invitations(self, force=False):
        """Ask GitHub again, off the main thread, and tell everyone watching.

        `force` skips the conditional request. Opening the requests window
        forces one: that is the moment somebody is actually looking, and it is
        the moment a stale cached answer would be most visibly wrong - which is
        exactly how round 7 found it, the badge saying 2 while the list that
        opened underneath it held 3.
        """
        def work():
            try:
                invites = pending_invitations(force=force)
                if invites is not None:
                    self._report_cleared(clear_expired_invitations(), [])
            except Exception as exc:
                # Network trouble is already handled inside, which returns
                # None; anything reaching here is a fault in this program, and
                # swallowing it makes a broken poll look exactly like a poll
                # that found nothing - silently, for as long as the app is
                # open. Say it once rather than crash the thread.
                if not getattr(self, "_invite_fault", None):
                    self._invite_fault = True
                    self.lines.put(f"the invitation check is failing: {exc}")
                return
            self._invite_fault = False
            self.post(lambda: self._set_invitations(invites))
        threading.Thread(target=work, daemon=True).start()

    def _invite_tick(self):
        """Look for new invitations about once a minute, for as long as the
        app is open. A conditional request makes an unchanged answer free, so
        the button can un-grey by itself without anyone pressing anything."""
        self.refresh_invitations()
        self.after(60000, self._invite_tick)

    def _show_conflicts(self):
        ConflictReportsWindow(self, self)

    def refresh_destructive(self, urgent=True):
        """Light the button when something is being held, and only then.

        The looking happens on a worker thread and only the painting here. It
        used to run on this thread, every four seconds, and on a real project
        one look took longer than four seconds - so the next was always
        already due and the window never answered a click again. Whatever the
        project's size, the window now stays alive; a look that is still out
        is not stacked on, and a request that arrives meanwhile (a "put them
        back", a confirmation) is run as soon as it finishes, not dropped.
        """
        repo = self.repo
        if not repo or not os.path.isdir(repo):
            return
        if self._destructive_busy:
            if urgent:
                self._destructive_again = True
            return
        self._destructive_busy = True

        def work():
            try:
                found = read_destructive(repo)
            except Exception:
                found = None         # could not look: keep what was last known
            self.post(lambda: self._destructive_done(repo, found))
        threading.Thread(target=work, daemon=True).start()

    def _destructive_done(self, repo, found):
        self._destructive_busy = False
        if found is not None and repo == self.repo:
            self._apply_destructive(*found)
        if self._destructive_again:
            self._destructive_again = False
            self.refresh_destructive()

    def _apply_destructive(self, changes, approved, large=None, large_ok=""):
        held = list(changes.get("deleted", [])) + list(changes.get("reverted", []))
        # Already confirmed is not "waiting for you" - the button must not sit
        # lit after the person has answered.
        if held and approved and approved == destructive_signature(changes):
            held = []
        self._destructive = changes if held else {"deleted": [], "reverted": []}
        large = list(large or [])
        if large and large_ok and large_ok == large_signature(large):
            large = []
        self._large = large
        n = len(held) + len(large)
        if n:
            self.btn_destructive.configure(text="Needs your OK (%d)" % n)
            self.btn_destructive.state(["!disabled"])
        else:
            self.btn_destructive.configure(text="Needs your OK")
            self.btn_destructive.state(["disabled"])
        self._paint_now()

    def _show_destructive(self):
        """What waits for the person's word: destroying work first - it is the
        one that loses something - then large new content."""
        changes = getattr(self, "_destructive", None)
        if changes and (changes.get("deleted") or changes.get("reverted")):
            DestructiveWindow(self, self, changes)
        elif getattr(self, "_large", None):
            LargeWindow(self, self, self._large)
        else:
            messagebox.showinfo("Nothing is held",
                                "Nothing is waiting for your word right now.", parent=self)

    def _add_people(self, repo=None):
        """Invite people to ONE project - the open one, or one chosen first."""
        target = repo or self.repo
        if not target:
            # From the welcome screen there is no open project, so the
            # question "which one?" has to be answered before anything else.
            # Offering the last-used project silently would invite people to a
            # project the person is not looking at.
            known = [e for e in self.cfg.get("projects", [])
                     if os.path.isdir(os.path.join(e.get("path", ""), ".git"))]
            if not known:
                messagebox.showinfo(
                    APP_NAME,
                    "There is no project on this machine to add people to." + NN +
                    "Start a shared project first, or join one.")
                return
            dlg = ProjectChooser(self, known)
            self.wait_window(dlg)
            if not dlg.result or dlg.result[0] != "open":
                return
            target = dlg.result[1]
        AddPeopleWindow(self, self, repo=target)

    def _open_requests(self):
        # Ask outright, not conditionally. Somebody is looking now, and a
        # cached answer that has gone stale is worst at exactly this moment.
        self.refresh_invitations(force=True)
        RequestsWindow(self, self, lambda inv: self._setup("friend", invite=inv))

    def _join(self):
        """The join button: offer the waiting requests, or the manual way."""
        JoinChooser(self, self, self._open_requests, lambda: self._setup("friend"))

    def _show_welcome(self):
        self.mgmt_row.pack_forget()
        self.home_row.pack_forget()
        self.req_row.pack_forget()
        self.partner_row.pack_forget()
        self.project.pack_forget()
        self.welcome.pack(fill="both", expand=True)
        self.caption_lbl.config(text="")
        self.title_lbl.config(text=APP_NAME)
        self.path_lbl.config(text="no project open")
        self.pill.set("idle", MUTED)

    def _show_project(self, remember=True):
        self.mgmt_row.pack(side="right")
        self.home_row.pack(side="left", padx=(10, 0), after=self.help_btn)
        self.req_row.pack(side="left", padx=(10, 0), after=self.home_row)
        self.partner_row.pack(anchor="e", after=self.status_row)
        # Who is here, and what is held, belong to ONE project. Opening a
        # different one used to leave the last project's people on screen
        # until the next four-second look came back - the user's rule is that
        # this corner shows the open project and nothing else. So: wipe it,
        # say it is being read, and read it now rather than at the next tick.
        if self.repo != self._painted_repo:
            self._painted_repo = self.repo
            self._partner_snap = None
            self.team.clear()
            self._apply_destructive({"deleted": [], "reverted": []}, "")
            self.after_idle(self._poll_once)
        self.missing.pack_forget()
        self.welcome.pack_forget()
        self.project.pack(fill="both", expand=True)
        # Remember it the moment it is opened, not only when it was set up. Then
        # closing the window on a project always reopens on that project.
        #
        # But NOT while it is still being set up. A folder whose setup failed
        # used to be remembered anyway, as the project to reopen - measured on
        # 2026-10-01: a share that could not complete left that folder as the
        # last project, so every start of the app opened a folder that was
        # never shared, and the window froze on it. It is remembered when the
        # setup succeeds (_finish_setup), and not before.
        if self.repo and remember:
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

    def post(self, fn):
        """Run fn on the main thread. Safe to call from any thread."""
        self._jobs.put(fn)

    def _run_jobs(self):
        """Whatever the background threads handed back, run here."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            try:
                job()
            except Exception:
                pass          # one bad hand-off must not stop the rest

    def _drain(self):
        self._run_jobs()
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
        return ENGINES.running(self.repo)

    def start_sync(self):
        if not self.repo:
            return
        repo = self.repo
        if ENGINES.stopping(repo):
            self.say("the sync engine is still stopping - start it again once it has", "warn")
            self._paint_sync_button()
            return
        if ENGINES.replacing(repo):
            # The startup sweep is already moving this engine onto this build.
            self._keep_intent(repo, True)
            self.say("the sync engine is being restarted on " + latin(APP_VERSION)
                     + " - it carries on in a moment", "warn")
            self._paint_sync_button()
            return
        ours = ENGINES.ours(repo)
        pid = None if ours is not None else daemon_pid(repo)
        if ours is None and pid is not None and turned_off(self.cfg, repo):
            # The person turned it off, and the engine still running is that
            # stop on its way - asked before the app was closed or updated,
            # not read yet. Carried through, never taken back (turned_off).
            self._carry_stop(repo)
            return
        if ours is not None or pid is not None:
            # An engine from a previous window is still covering this folder -
            # but an engine deliberately outlives windows, which means it can
            # outlive UPDATES and keep running last week's code with none of
            # the new behavior, silently. Field-found: a leftover engine ran
            # 2.0.7 rules under a 2.0.15 app. This version, this build, this
            # folder: adopt it. Anything else - an engine before 2.0.16 says
            # no version at all - is replaced with one running this build's
            # code. One this app started itself is current by construction,
            # heartbeat or not. A heartbeat that came along with a MOVED folder
            # vouches for an engine still working on the old path: replaced too.
            current, why = (True, "") if ours is not None else engine_current(repo, pid)
            self._keep_intent(repo, True)       # it runs: this project is syncing
            if current:
                # A replacement's request left behind - the app went before
                # the engine read it - is taken back: this engine is current.
                # (The person's stop never reaches here: turned_off, above.)
                withdraw_stop_request(repo)
                self.say("sync engine already running in the background - reconnected", "ok")
                self._paint_sync_button()
                return
            if why == "place":
                self.say("this folder was moved while its engine ran - that engine "
                         "stops at its old place, and syncing starts here", "warn")
            elif why == "build":
                self.say("engine was running an earlier build of " + latin(APP_VERSION)
                         + " - restarting it on this one", "warn")
            else:
                self.say("engine was running older code - restarting it on "
                         + latin(APP_VERSION), "warn")
            # Off this thread, the same way the startup sweep does it: the old
            # engine is stopped only at rest between two passes, never in the
            # middle of its work - a wait the window must not spend frozen.
            if ENGINES.claim(repo):
                threading.Thread(target=self._replace_worker, args=(repo, True),
                                 daemon=True).start()
            self._paint_sync_button()
            return
        # Nothing runs here. A project the person turned off stays off - after
        # a restart of the app or of the machine as much as after going Home -
        # until they turn it on: Start sync records that before it calls this.
        if turned_off(self.cfg, repo):
            self._keep_intent(repo, False)      # a stop asked before 2.2.2: kept
            self.say("syncing of this project is off - you turned it off; "
                     "Start sync turns it on again", "warn")
            self._paint_sync_button()
            self.pill.set("stopped", MUTED)
            return
        if not os.path.exists(engine_script()):
            explain('The sync engine could not be found.',
                    'A file that ships inside the program is not where it should be. Usually the program file was copied incompletely, or antivirus quarantined part of it.',
                    'Close the app and open it again. If it keeps happening, get a fresh copy from Amin.')
            return
        try:
            ENGINES.start(repo)
        except Exception as exc:
            explain('Syncing could not be started.',
                    f'Windows refused: {exc}',
                    'Close the app and open it again. If it repeats, check that git is installed - run  git --version  in a terminal.')
            return
        self._keep_intent(repo, True)
        self._paint_sync_button()
        self.pill.set("syncing", ACCENT)
        self.say("sync engine started", "ok")

    def _keep_intent(self, repo, on):
        """Record whether the person left this project syncing (sync_intent)."""
        if repo and set_sync_intent(self.cfg, repo, on):
            save_config(self.cfg)

    def stop_sync(self, quiet=False, then=None):
        # The person turned it off: it stays off - across a restart of the
        # app, or of the machine - until they turn it on again.
        self._keep_intent(self.repo, False)
        self._stop_project(self.repo, quiet=quiet, then=then)

    def _stop_project(self, repo, quiet=False, then=None):
        """Turn syncing off for one project - the open one, or one being moved
        or taken off the list - and for that project only (see Engines).

        Its engine stops at rest, never in the middle of a git command: asked
        to stop itself when it understands that, watched from outside and
        ended in its rest when it does not. That takes a few seconds, so the
        window says "Stopping..." meanwhile and stays usable. `then(outcome)`
        runs on this thread once the engine has gone.
        """
        if not repo:
            if then:
                then("none")
            return
        # Tell the other side we are going - an engine ended from outside
        # cannot announce its own departure.
        if ENGINES.running(repo):
            threading.Thread(target=clear_my_presence,
                             args=(repo, presence_name(repo)), daemon=True).start()

        def done(outcome):
            self.post(lambda: self._stopped(repo, outcome, quiet, then))
        if not ENGINES.stop(repo, done=done):
            self._stopped(repo, "none", quiet, then)
            return
        if not quiet:
            self.say("stopping sync - the engine first finishes what it is in the middle of", "warn")
        if repo == self.repo:
            self.pill.set("stopping...", WARN)
            self._paint_sync_button()

    def _stopped(self, repo, outcome, quiet, then):
        name = os.path.basename(repo.rstrip("\\/")) or repo
        if outcome == "killed":
            self.say(f"the sync engine of {name} never came to rest in {REPLACE_WAIT} s, "
                     f"so it was stopped where it stood", "warn")
        if repo == self.repo:
            self._paint_sync_button()
            if not self.sync_running():
                self.pill.set("stopped", MUTED)
        if outcome != "none" and not quiet:
            self.say("sync stopped - nothing is sent or received until you start it again", "warn")
        if then:
            then(outcome)

    def _paint_sync_button(self):
        """Stop sync, Start sync - or Stopping..., and not pressable meanwhile."""
        if self.repo and ENGINES.stopping(self.repo):
            self.btn_sync.config(text="Stopping...")
            self.btn_sync.state(["disabled"])
            return
        self.btn_sync.state(["!disabled"])
        self.btn_sync.config(text="Stop sync" if self.sync_running() else "Start sync")

    def _sweep_engines(self):
        """At start: move every engine still running older code onto this build.

        An engine deliberately outlives its window, so it outlives UPDATES too,
        and start_sync replaces one only when its own project is opened. Found
        2026-10-01: the engine of a test project ran 2.1.9 code - marker refs
        pushed from HEAD, carrying unsent commits to GitHub - for a day under
        2.2.0, because nobody opened that project. An update restarts the app,
        so this runs after every update as well. All the looking and waiting
        happens off this thread; the window only prints the result.

        And every project the person LEFT syncing gets its engine back if none
        runs - the user's ruling of 2026-10-03: a restart of the machine ends
        every engine, and each project that was syncing carries on, not only
        the last one opened. One they turned off stays off (turned_off), and
        an engine still running there is their stop on its way: carried
        through, never replaced. An engine found running where nothing is
        recorded marks its project as syncing: that is how the first start of
        2.2.2 learns what was on.
        """
        listed = [e.get("path", "") for e in self.cfg.get("projects", []) if e.get("path")]
        threading.Thread(target=self._sweep_worker, args=(listed,), daemon=True).start()

    def _sweep_worker(self, listed):
        """Worker thread: only the slow look - which engines run older code.
        What is done about it is decided on the main thread (_after_sweep),
        where the person's own Stop and Start run: a decision taken here
        could be overtaken by a Stop pressed meanwhile, and bring an engine
        back behind it."""
        try:
            stale = stale_engines(listed)
        except Exception as exc:
            self.lines.put(f"could not check the sync engines of the other projects: {exc}")
            stale = []
        self.post(lambda: self._after_sweep(stale))

    def _after_sweep(self, stale):
        """Main thread: replace, carry a stop through, resume - per project."""
        for repo in stale:
            if turned_off(self.cfg, repo):
                self._carry_stop(repo)
            elif ENGINES.claim(repo):   # the window may have just begun on it
                threading.Thread(target=self._replace_worker, args=(repo,),
                                 daemon=True).start()
        seen, changed = set(), False
        for entry in list(self.cfg.get("projects", [])):
            path = entry.get("path", "")
            try:
                if (not path or not os.path.isdir(os.path.join(path, ".git"))
                        or not has_teamsync_files(path)):
                    continue        # moved, removed, or not ours: nothing to resume
                key = _engine_key(path)
                if key in seen:
                    continue        # one folder listed twice is one project
                seen.add(key)
                if ENGINES.running(path):
                    if ENGINES.stopping(path) or ENGINES.replacing(path):
                        continue
                    if turned_off(self.cfg, path):
                        self._carry_stop(path)
                    elif sync_intent(self.cfg, path) is None:
                        changed = set_sync_intent(self.cfg, path, True) or changed
                    continue
                if sync_intent(self.cfg, path) is True and not ENGINES.is_closing():
                    ENGINES.start(path)
                    name = os.path.basename(path.rstrip("\\/")) or path
                    self.say(f"resumed syncing of {name} - it was left syncing", "ok")
            except Exception as exc:
                self.say(f"could not resume syncing of {path}: {exc}", "warn")
        if changed:
            save_config(self.cfg)
        if self.repo:
            self._paint_sync_button()

    def _carry_stop(self, repo):
        """An engine still running on a project the person turned off is their
        stop on its way, not read yet (turned_off). It goes through - at rest,
        as every stop - and nothing takes it back or starts a fresh engine."""
        if ENGINES.stopping(repo):
            return
        self._keep_intent(repo, False)          # a stop asked before 2.2.2: kept
        name = os.path.basename(repo.rstrip("\\/")) or repo
        self.say(f"you turned syncing of {name} off, and its engine was still running - "
                 f"it stops once it has finished what it is in the middle of", "warn")

        def then(outcome):
            if outcome != "none":
                self.say(f"the sync engine of {name} has stopped", "warn")
        self._stop_project(repo, quiet=True, then=then)

    def _replace_worker(self, repo, start_anyway=False):
        """Worker thread: one engine moved onto this build, one line about it."""
        try:
            outcome, old, detail = ENGINES.replace(repo, start_anyway=start_anyway)
        except Exception as exc:
            outcome, old, detail = "failed", "", str(exc)
        name = os.path.basename(repo.rstrip("\\/")) or repo
        was = ("v" + old) if old else "an older version"
        now = "v" + APP_VERSION
        if outcome == "restarted" and detail == "place":
            # It worked on the folder's old place (moved while it ran).
            line, tag = f"the sync engine of {name} now works from the folder's new place", "ok"
        elif outcome == "restarted" and detail == "build":
            line, tag = (f"restarted the sync engine of {name} on this build of {now} - "
                         f"it was running an earlier build of it"), "ok"
        elif outcome == "restarted":
            line, tag = (f"restarted the sync engine of {name} on {now} - "
                         f"it was still running {was}"), "ok"
        elif outcome == "started":
            line, tag = (f"the old sync engine of {name} had stopped by itself - "
                         f"started one on {now}"), "ok"
        elif outcome == "busy":
            line, tag = (f"the sync engine of {name} is still on {was}: it was at work "
                         f"the whole {REPLACE_WAIT} s, and an engine is never stopped in the "
                         f"middle of its work - it is replaced at the next start of the app, "
                         f"or when {name} is opened"), "warn"
        elif outcome == "failed":
            line, tag = (f"could not restart the sync engine of {name} ({detail}) - it "
                         f"runs on as before, on {was}"), "warn"
        elif outcome == "lost":
            line, tag = (f"the sync engine of {name} was stopped to move it to {now}, but "
                         f"the new one could not be started: {detail} - {name} is NOT "
                         f"syncing now; open it to start syncing again"), "bad"
        elif outcome == "died":
            line, tag = (f"restarted the sync engine of {name} on {now}, but the new one "
                         f"stopped at once ({detail}) - {name} is NOT syncing now; open "
                         f"it to see why"), "bad"
        elif outcome == "slow":
            line, tag = (f"restarted the sync engine of {name} on {now}, but it has not "
                         f"reported back in {ENGINE_UP_WAIT} s - open {name} to check "
                         f"it is syncing"), "warn"
        else:
            return          # nothing to replace, or the person stopped it
        self.post(lambda: self.say(line, tag))

    def toggle_sync(self):
        if self.repo and ENGINES.stopping(self.repo):
            return                  # already on its way off; the button says so
        if not self.sync_running():
            # The person turns it on - which is what lifts an earlier Stop.
            self._keep_intent(self.repo, True)
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

        A moved folder used to mean closing the window, or going Home - and that
        can leave a dead path behind. This changes the location in place: the old
        engine is stopped, the new one starts where the files actually are.

        In that order, and the second only once the first is done: the old
        engine stops at rest, which takes a moment, and one worktree gets one
        engine. A folder MOVED while its engine ran leaves that engine working
        on a path that no longer exists - it stops by itself within seconds
        (measured: two), and start_sync waits it out.
        """
        old = self.repo
        p = self._pick_project("Where is the project now?")
        if not p:
            return
        if os.path.normcase(p) == os.path.normcase(old or ""):
            messagebox.showinfo(APP_NAME, "That is the current location - nothing changed.")
            return
        if not has_teamsync_files(p):
            messagebox.showwarning(
                APP_NAME, "That folder is not this project - it has none of the files "
                          "every shared project carries (push-now.ps1, "
                          "TEAM-PROJECT-REFERENCE.md)." + NN + p + NN + "Nothing was changed.")
            return
        # The project moved - it did not become a second project. Drop the old
        # entry, or the list keeps a dead row pointing at a folder that is gone.
        # Whether it syncs did not move: that goes along with it.
        was = sync_intent(self.cfg, old) if old else None
        if old:
            forget_project(self.cfg, old)
        self.repo = p
        self.cfg["last_project"] = p
        remember_project(self.cfg, p)
        if was is not None:
            set_sync_intent(self.cfg, p, was)
        save_config(self.cfg)
        self._show_project()
        self.say(f"project location changed to {p}", "warn")

        def then(_outcome):
            if self.repo == p:
                self.start_sync()
        self._stop_project(old, quiet=True, then=then)

    def _switch(self):
        """Home: back to the first screen.

        Leaves the engine running: going home is not a decision to stop
        syncing. Stopping is always explicit.
        """
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
        repo = self.repo
        running = ENGINES.running(repo)
        forget_project(self.cfg, repo)
        if self.cfg.get("last_project") == repo:
            self.cfg.pop("last_project", None)
        save_config(self.cfg)
        self.say(f"disconnected from {name}" + (
            " - its sync engine stops as soon as it has finished what it is in "
            "the middle of" if running else ""), "warn")
        self.repo = ""
        self._show_welcome()

        def then(outcome):
            if outcome != "none":
                self.say(f"the sync engine of {name} has stopped", "warn")
        self._stop_project(repo, quiet=True, then=then)

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

    def _show_note(self, text, kind, path=None):
        """The note above the first screen's buttons, with the ONE action that
        fits what it says.

        Two kinds of news share this place but never a button. A project whose
        folder MOVED is found again: "Locate the folder". A folder that was
        NEVER SHARED is shared: "Share this folder". The first build of 2.2.0
        wrote the never-shared note into the moved-folder box, and the red
        Locate button came along with it - asking the person to go and find a
        folder that was not lost. The user caught it on the first look and
        asked what the button was for, which was the right question.
        """
        self.missing_lbl.config(text=text)
        self.missing_locate.pack_forget()
        self.missing_share.pack_forget()
        self._never_shared_path = path if kind == "never_shared" else None
        chosen = self.missing_share if kind == "never_shared" else self.missing_locate
        chosen.pack(anchor="w", pady=(10, 0))
        self.missing.pack(fill="x", pady=(0, 18), before=self.welcome_actions)

    def _share_never_shared(self):
        """The never-shared note's button: the share form, for that folder."""
        if self._never_shared_path:
            self._setup("owner", path=self._never_shared_path)

    def _locate_missing(self):
        """The welcome screen's shortcut for a project whose folder moved."""
        gone = self.cfg.get("last_project")
        p = self._pick_project("Where is the project now?")
        if not p:
            return
        if not self._is_ours_or_offer_share(p):
            return
        was = sync_intent(self.cfg, gone) if gone else None     # it goes along
        if gone and os.path.normcase(gone) != os.path.normcase(p):
            forget_project(self.cfg, gone)
        remember_project(self.cfg, p)
        if was is not None:
            set_sync_intent(self.cfg, p, was)
        self.repo = p
        self.cfg["last_project"] = p
        save_config(self.cfg)
        self._show_project()
        self.start_sync()

    def _is_ours_or_offer_share(self, path):
        """True when the folder is a project this app set up or joined.

        Otherwise it is never opened as one - opening starts the engine, and
        the engine used to sync straight into whatever 'origin' the folder
        had, a person's own public repository included. The way forward is
        offered instead: sharing it makes a NEW private project.
        """
        if has_teamsync_files(path):
            return True
        if messagebox.askyesno(
                APP_NAME,
                "This folder is not a shared project yet:" + NN + path + NN +
                "The app only syncs projects it set up, so it will not open this "
                "one as it is. Share it now? That makes a new private project for "
                "it; any repository it already has is left alone."):
            self._setup("owner", path=path)
        return False

    def _open_existing(self):
        """Show what has been opened before. Browsing is the fallback, not the ritual."""
        dlg = ProjectChooser(self, self.cfg.get("projects", []),
                             running=unlisted_engines(self.cfg))
        self.wait_window(dlg)
        if not dlg.result:
            return
        action, path = dlg.result

        if action == "forget":
            self._forget_project(path)
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
        p = os.path.normpath(path)
        if not self._is_ours_or_offer_share(p):
            return
        self.repo = p
        self.cfg["last_project"] = self.repo
        save_config(self.cfg)
        self._show_project()
        self.start_sync()

    def _forget_project(self, path):
        """The chooser's Remove from list.

        It used to take the project off the list and nothing else - and an
        engine running for it ran on, out of sight, with no way back to it
        but Browse. A test project synced like that for a day. So when its
        engine runs, the person is asked whether its syncing stops too.
        """
        name = os.path.basename(path.rstrip("\\/")) or path
        listed = any(os.path.normcase(os.path.normpath(e.get("path", "")))
                     == os.path.normcase(os.path.normpath(path))
                     or _same_folder(e.get("path", ""), path)
                     for e in self.cfg.get("projects", []) if e.get("path"))
        if ENGINES.running(path):
            if listed:
                answer = messagebox.askyesnocancel(
                    APP_NAME,
                    f"{name} is still syncing in the background." + NN +
                    "Yes: stop syncing it too, and remove it from the list." + N +
                    "No: only remove it from the list - it keeps syncing, out of sight, "
                    "until this machine restarts (only listed projects are resumed)." + N +
                    "Cancel: do nothing.")
            else:
                answer = messagebox.askyesno(
                    APP_NAME,
                    f"{name} is not on your list, but it is still syncing in the "
                    "background." + NN + "Stop syncing it?") or None
            if answer is None:
                return
            if answer:
                def then(outcome):
                    if outcome != "none":
                        self.say(f"the sync engine of {name} has stopped", "warn")
                self.say(f"stopping the sync engine of {name} - it first finishes "
                         "what it is in the middle of", "warn")
                self._stop_project(path, quiet=True, then=then)
        if listed:
            forget_project(self.cfg, path)
            save_config(self.cfg)
            self.say(f"removed {path} from the list - nothing on disk was touched", "warn")

    # -- setup -------------------------------------------------------------

    def _setup(self, mode, invite=None, path=None):
        dlg = SetupDialog(self, mode, self.cfg, invite=invite, path=path)
        self.wait_window(dlg)
        if not dlg.result:
            return
        v = dlg.result
        for key in ("me", "email", "friend", "owner"):
            if v.get(key):
                self.cfg[key] = v[key]
        for login in v.get("people", []):
            remember_person(self.cfg, login)
        save_config(self.cfg)

        if mode == "owner":
            # First, is this folder already shared - here or anywhere? Then
            # the people go to THAT project and no second one is made. Asked
            # off the main thread: it is a few round trips to GitHub.
            folder = os.path.normpath(v["path"])
            self.configure(cursor="watch")

            def work():
                try:
                    route = share_route(folder)
                except Exception:
                    route = ("new", "")
                self.post(lambda: self._share_routed(v, route))
            threading.Thread(target=work, daemon=True).start()
            return
        self._run_setup(mode, v, invite)

    def _share_routed(self, v, route):
        self.configure(cursor="")
        kind, slug = route
        folder = os.path.normpath(v["path"])
        people = list(v.get("people", []))
        if kind == "shared":
            # This very folder is a shared project already. Open it and add the
            # people to it - setting it up again would make a second project
            # for the same work.
            self.repo = folder
            self._show_project()
            if not self.sync_running():
                self.start_sync()
            self.say(f"{os.path.basename(folder)} is already shared as {slug} - "
                     "the people are added to it; no second project is made", "warn")
            if people:
                self._invite_into(slug, people)
            return
        if kind == "elsewhere":
            answer = messagebox.askyesnocancel(
                APP_NAME,
                "This folder's history is already shared, as " + slug + NN +
                "- set up from another machine, or from an earlier copy of this folder." + NN +
                "Yes: add the people to " + slug + ". This folder stays exactly as it "
                "is; to work in the shared project on this machine, join it from the "
                "first screen." + NN +
                "No: make a separate, new shared project from this folder anyway." + NN +
                "Cancel: do nothing.")
            if answer is None:
                return
            if answer:
                if people:
                    self._invite_into(slug, people, box=True)
                else:
                    messagebox.showinfo(APP_NAME, "Nobody was ticked, so nobody was invited.")
                return
        self._choose_what_to_share(v)

    def _choose_what_to_share(self, v):
        """Before a NEW project is made: keep something on this machine, or
        share automatically? (the user's design, 2026-10-01). Either way the
        folder is measured first, and large content is never uploaded without
        the person having seen it."""
        folder = os.path.normpath(v["path"])
        answer = messagebox.askyesnocancel(
            APP_NAME,
            "Before " + os.path.basename(folder) + " is shared:" + NN +
            "Do you want to keep some files or folders on this machine only?" + NN +
            "Yes: choose them - you see everything that would be shared, with sizes." + NN +
            "No: share automatically. The folder's .gitignore applies, and what tools "
            "rebuild (node_modules, Python caches) stays home." + NN +
            "Cancel: do nothing.")
        if answer is None:
            return
        self.configure(cursor="watch")
        self.say("measuring what would be shared...")

        def work():
            pv = share_preview(folder, [])
            self.post(lambda: self._share_measured(v, bool(answer), pv))
        threading.Thread(target=work, daemon=True).start()

    def _share_measured(self, v, choose, pv):
        self.configure(cursor="")
        if pv.get("error"):
            messagebox.showerror(APP_NAME, "Could not measure what would be shared - nothing "
                                           "was changed:" + NN + pv["error"][:800])
            return
        if not choose and pv.get("large"):
            answer = messagebox.askyesnocancel(
                APP_NAME,
                "This would upload LARGE content - once uploaded it stays in the "
                "project's history for good:" + NN +
                "\n".join("  %s  %s%s" % (h["item"], size_text(h["bytes"]),
                                           "  (already in the folder's history)"
                                           if h.get("committed") else "")
                           for h in pv["large"][:12]) + NN +
                "Yes: choose what stays on this machine." + N +
                "No: upload it anyway." + N +
                "Cancel: do nothing.")
            if answer is None:
                return
            if answer is False:
                v["allow_large"] = True
                self._run_setup("owner", v, None)
                return
            choose = True
        if not choose:
            self._run_setup("owner", v, None)
            return
        win = ExclusionWindow(self, self, v["path"], pv)
        self.wait_window(win)
        if not win.result:
            self.say("sharing cancelled - nothing was changed")
            return
        v["exclude"] = win.result["keep"]
        v["allow_large"] = win.result["allow_large"]
        self._run_setup("owner", v, None)

    def _invite_into(self, slug, people, box=False):
        """Invite people to an existing project, off the main thread."""
        def work():
            allowed = repo_admin(slug)
            if allowed:
                said = [invite_to_repo(slug, login)[1] for login in people]
            elif allowed is None:
                said = ["GitHub could not be reached - nobody was invited. Try again in a moment."]
            else:
                said = [f"Only the person who created {slug} can add people to it - ask them."]

            def show():
                for line in said:
                    self.say(line, "warn")
                if box:
                    messagebox.showinfo(APP_NAME, "\n".join(said))
            self.post(show)
        threading.Thread(target=work, daemon=True).start()

    def _run_setup(self, mode, v, invite):
        script = resource_path("engine", "init-owner.ps1" if mode == "owner" else "init-friend.ps1")
        cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-NoWatch"]
        if mode == "owner":
            cmd += ["-Path", v["path"]]
            if v.get("reponame"):
                cmd += ["-RepoName", v["reponame"]]
            if v.get("friend"):
                cmd += ["-Friend", v["friend"]]
            if v.get("exclude"):
                cmd += ["-Exclude", exclude_arg(v["exclude"])]
            if v.get("allow_large"):
                cmd += ["-AllowLarge"]
        else:
            cmd += ["-RepoName", v["reponame"], "-Path", v["path"]]
            if v.get("owner"):
                cmd += ["-Owner", v["owner"]]
        if v.get("me"):
            cmd += ["-Me", v["me"]]
        if v.get("email"):
            cmd += ["-MyEmail", v["email"]]

        self.repo = os.path.normpath(v["path"])
        # Shown, so the setup's own messages can be read as they come - but
        # not remembered until it has succeeded (see _show_project), and not
        # read by the four-second tick while the setup is still writing it.
        self._setting_up = True
        self._show_project(remember=False)
        self.say("setting up...", "warn")
        self.pill.set("setting up", WARN)
        threading.Thread(target=self._finish_setup, args=(cmd, invite), daemon=True).start()

    def _finish_setup_prelude(self, invite):
        """Accept the invitation, if this setup came from one.

        Deliberately here and not at the click: until a folder has been
        chosen there is nothing to download into, and an invitation accepted
        for a cancelled dialog would be spent - it disappears from GitHub and
        the person is left a collaborator with no copy and no button to press.
        """
        if not invite:
            return True
        self.lines.put(f"accepting the invitation to {invite['full']}...")
        if accept_invitation(invite["id"]):
            # Off the count immediately - it is no longer waiting, whatever
            # the download does next.
            self.drop_invitation(invite["id"])
            # "Accepted" is not proof of access. Measured 2026-10-01: an
            # invitation that had expired was "accepted" with a success
            # answer, vanished, and added nobody - and the download then
            # said "you have probably not accepted the invitation yet", two
            # seconds after this very line had accepted it. Look first.
            if not repo_visible(invite["full"]):
                self.lines.put(
                    f"GitHub took the answer but gave no access to {invite['full']}. "
                    f"The invitation had most likely expired - they last seven days. "
                    f"Ask {invite['inviter']} to invite you again; nothing was downloaded.")
                return False
            self.lines.put("invitation accepted")
            return True
        self.lines.put("GitHub would not accept the invitation. It may have been "
                       "withdrawn, or already taken on github.com.")
        # It failed because it is gone: withdrawn, or answered elsewhere.
        # Either way it must stop being counted.
        self.drop_invitation(invite["id"])
        return False

    def _finish_setup(self, cmd, invite=None):
        # Runs on a worker thread: everything that touches the window or the
        # config goes back to the main thread through post().
        if not self._finish_setup_prelude(invite):
            self.post(self._setup_failed)
            return
        code = self._run_and_log(cmd)
        if code == 0 and os.path.isdir(os.path.join(self.repo, ".git")):
            self.post(self._setup_succeeded)
            # The new project brings new people with it, and the invitation
            # just taken is no longer waiting. Re-ask rather than guess.
            threading.Thread(target=self._probe_account, daemon=True).start()
        else:
            self.lines.put("setup did not finish. Read the messages above.")
            self.post(self._setup_failed)

    def _setup_succeeded(self):
        """Only now is the folder a project worth reopening."""
        self._setting_up = False
        remember_project(self.cfg, self.repo)
        set_sync_intent(self.cfg, self.repo, True)     # shared or joined: it syncs
        self.cfg["last_project"] = self.repo
        save_config(self.cfg)
        self.lines.put("setup finished - starting sync")
        self.start_sync()

    def _setup_failed(self):
        # Nothing was remembered, so the next start of the app does not come
        # back here. The messages stay on screen; Home leads away.
        self._setting_up = False
        self.pill.set("not shared - setup did not finish", BAD)

    # -- status ------------------------------------------------------------

    def _poll_git(self):
        """The four-second tick. One chain of these runs for the life of the
        window; anything that wants a look NOW calls _poll_once, never this,
        or it would start a second chain."""
        self.after(4000, self._poll_git)
        self._poll_once()

    def _poll_once(self):
        if self.repo and not os.path.isdir(self.repo):
            # Moved or deleted while open. Fall back to the welcome screen and
            # explain, rather than showing a project that is not there.
            gone = self.repo
            self.repo = ""
            self.say(f"the project folder is no longer at {gone}", "bad")
            self._show_welcome()
            self._show_note(
                f"The project folder is no longer at this path:\n{gone}\nUse \"Open a project already on this machine\" to point at its new location.",
                "moved")
            return
        if not self.repo or not os.path.isdir(os.path.join(self.repo, ".git")):
            return
        # Nothing to read until the folder IS a shared project: not while a
        # setup is still writing it, and not after one that did not finish.
        if self._setting_up or not has_teamsync_files(self.repo):
            return
        # Held destructive changes ride the same tick, on their own worker.
        # The engine has already refused to publish them; without this the
        # person would only learn it from the log, the one place a paused
        # publish is easy to miss. Not urgent: if a look is still out, the
        # next tick starts the next one - only a person's own request (put
        # back, confirm) queues one straight after.
        self.refresh_destructive(urgent=False)
        # Everything else is READ on a worker thread too and only painted
        # here. These reads used to run on this thread - a dozen git processes
        # every four seconds - and the window is the one thing that must
        # never wait on git.
        if self._poll_busy:
            return
        self._poll_busy = True
        repo = self.repo

        def work():
            try:
                snap = read_project_state(repo)
            except Exception:
                snap = None
            self.post(lambda: self._poll_done(repo, snap))
        threading.Thread(target=work, daemon=True).start()

    def _poll_done(self, repo, snap):
        self._poll_busy = False
        if snap is None or repo != self.repo:
            return
        # Before the conflict check, not after it. A conflict of our own used
        # to freeze this display for as long as it lasted, so the teammate
        # who was still working looked frozen too - and their own conflict
        # warning could never appear here.
        self._apply_partner(snap)

        unmerged = snap["unmerged"]
        if unmerged:
            n = len(unmerged.splitlines())
            self.banner.pack(fill="x", padx=18, pady=(0, 10), before=self.log.master)
            self.banner_lbl.config(
                text=f"Conflict in {n} file(s). Nothing was published, nothing was lost.\n"
                     f"Both versions are saved side by side for you.")
            self.pill.set("conflict", BAD)
            self.conflict_dir = newest_conflict_export(self.repo)
            return

        self.banner.pack_forget()
        self._paint_sync_button()
        if ENGINES.stopping(self.repo):
            self.pill.set("stopping...", WARN)
            return
        if not self.sync_running():
            self.pill.set("stopped", MUTED)
            return
        ahead = snap["ahead"] or "0"
        if snap["net"] == "offline":
            # Offline is not a failure state: work is committed locally and the
            # engine keeps retrying. Say that, rather than just showing a number.
            waiting = f" - {ahead} waiting" if ahead != "0" else ""
            self.pill.set(f"offline, retrying{waiting}", WARN)
        elif ahead != "0":
            self.pill.set(f"{ahead} not published yet", WARN)
        else:
            self.pill.set("everything published", OK)

    def _show_team(self):
        TeamWindow(self, self._team_people, self._team_activity)

    def _refresh_partner(self):
        """Who is here: green now, grey earlier, and nobody named twice.

        Reads and paints in one go, on whatever thread calls it - kept for a
        caller that needs the answer now. The tick itself goes through
        read_project_state on a worker and _apply_partner here.
        """
        self.refresh_destructive()
        self._apply_partner(read_partner_state(self.repo))

    def _apply_partner(self, snap):
        """Paint the team corner and the 'Right now' column from one look."""
        self._partner_snap = snap
        self._team_people = snap["people"]
        self._team_activity = snap["activity"]
        self.team.set(self._team_people, self._team_activity)
        self._paint_now()

    def _paint_now(self):
        """Everything that is TRUE RIGHT NOW goes into the column, one row per
        person. Conflicts first: a file somebody is untangling is the one
        file where another change from here makes their job harder and
        probably causes the next conflict. Their repository is fine and
        nothing is blocked - this is knowledge, not a lock. Then who is
        holding what, named per person rather than pooled: with two people
        "somebody is editing this" could only mean one person, and with five
        it answers a question nobody asked. Last, what is waiting for this
        person's own word.

        Called whenever either half arrives - the team's look or the
        destructive check, which run on separate workers - from the last
        answer of each.
        """
        snap = self._partner_snap or {}
        rows = []
        for who, files in (snap.get("conflicts") or {}).items():
            rows.append((BAD, "%s is resolving a conflict" % who, files))
        for who, files in (snap.get("pending") or {}).items():
            rows.append((WARN, "%s is working on" % who, files))
        held = self._destructive or {}
        n_held = len(held.get("deleted", [])) + len(held.get("reverted", []))
        if n_held:
            rows.append((BAD, "waiting for your word",
                         held.get("deleted", []) + held.get("reverted", [])))
        large = getattr(self, "_large", None) or []
        if large:
            rows.append((BAD, "large - waiting for your word",
                         ["%s  %s" % (h["item"], size_text(h["bytes"])) for h in large]))
        self._set_now(rows)

    def _set_now(self, rows):
        """Paint the 'Right now' column. Rebuilt only when it really changed.

        Tk has to destroy and recreate these widgets to redraw them, and this
        runs on the same tick as the rest of the panel - so a project where
        nothing is happening, which is most of the time, must cost nothing.
        """
        signature = repr(rows)
        if signature == self._now_signature:
            return
        self._now_signature = signature
        for child in self.nowarea.inner.winfo_children():
            child.destroy()
        if not rows:
            ttk.Label(self.nowarea.inner, text="nobody is holding anything",
                      font=FONT, foreground=MUTED, wraplength=260,
                      justify="left").pack(anchor="w")
            return
        for colour, title, files in rows:
            block = ttk.Frame(self.nowarea.inner)
            block.pack(fill="x", anchor="w", pady=(0, 8))
            ttk.Label(block, text=latin(title), font=("Segoe UI", 9, "bold"),
                      foreground=colour, wraplength=260,
                      justify="left").pack(anchor="w")
            # Every file, not a truncated run-on: the column has room to go
            # down, which is the whole reason it exists.
            for f in files:
                ttk.Label(block, text=latin("  " + f), font=MONO,
                          foreground=MUTED, wraplength=250,
                          justify="left").pack(anchor="w")

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
        # Announced once per version, not once per call: the 15-second check
        # re-shows the offer to keep the button in place, and on a machine
        # where the button sat untaken for hours that meant hundreds of
        # identical log lines. The button itself is the standing reminder;
        # the log line only marks the news.
        if getattr(self, "_offer_logged", None) != info["tag"]:
            self._offer_logged = info["tag"]
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
        # new code - the new copy's startup sweep sees to that, at rest.
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
        # No engine is stopped here - not this project's either. This used to
        # stop it first, "insurance" by its own record (STATE section 5): an
        # engine does not hold the folder being replaced. And that stop ended
        # it wherever it stood - a git child was running in 89-96% of samples
        # on the live engines, so nearly always in the middle of a command,
        # at every update. The new copy's startup sweep now moves every engine
        # onto it at rest instead. One caught mid-swap is finished first.
        ENGINES.close()
        if apply_update(self._update_ready):
            self.destroy()
            os._exit(0)          # the helper waits for this pid; die now, not at GC's leisure
        else:
            ENGINES.opened()
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
        # An engine the startup sweep has just stopped is started again on this
        # build before the process goes; one it has not reached is left as is.
        ENGINES.close()
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
