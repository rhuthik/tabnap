"""Process table, exact argv/env, cwd and memory, for macOS and Linux."""
import ctypes
import ctypes.util
import os
import re
import struct
import subprocess
import sys
import time


def run(argv, timeout=10):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_ps(out):
    """pid -> (ppid, tty, command) from `ps -axo pid=,ppid=,tty=,command=`."""
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    return table


def process_table():
    return parse_ps(run(["ps", "-axo", "pid=,ppid=,tty=,command="]))


def children_map(table):
    kids = {}
    for pid, (ppid, _tty, _cmd) in table.items():
        kids.setdefault(ppid, []).append(pid)
    return kids


def descendants(kids, pid):
    out, stack = [], [pid]
    while stack:
        for c in kids.get(stack.pop(), []):
            out.append(c)
            stack.append(c)
    return out


def tty_path(tty):
    """ps prints 'ttys003' on macOS and 'pts/3' on Linux; both live under /dev."""
    return None if tty in ("??", "?", "-", "") else f"/dev/{tty}"


def mem_mb(text):
    m = re.match(r"([\d.]+)([BKMG])", text)
    if not m:
        return 0.0
    return float(m.group(1)) * {"B": 1 / 1048576, "K": 1 / 1024, "M": 1, "G": 1024}[m.group(2)]


def parse_top(out):
    """pid -> MB from `top -l 1 -stats pid,mem` (macOS footprint incl. compressed; ps RSS undercounts)."""
    mem = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].isdigit():
            mem[int(p[0])] = mem_mb(p[1])
    return mem


def memory_table():
    """pid -> MB for every process. Slow on macOS (~1s), so call it from a worker thread."""
    if sys.platform == "darwin":
        return parse_top(run(["top", "-l", "1", "-stats", "pid,mem"], timeout=30))
    mem = {}
    for line in run(["ps", "-axo", "pid=,rss="]).splitlines():
        p = line.split()
        if len(p) == 2 and p[0].isdigit() and p[1].isdigit():
            mem[int(p[0])] = int(p[1]) / 1024
    return mem


_libc = None


def _sysctl(mib, size):
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(size)
    if _libc.sysctl(arr, len(mib), buf, ctypes.byref(n), None, ctypes.c_size_t(0)) != 0:
        return None
    return buf.raw[:n.value]


def parse_procargs2(data):
    """(argv, env) from a macOS KERN_PROCARGS2 buffer: argc, exec path, NUL padding, argv, env."""
    if not data or len(data) < 4:
        return [], {}
    argc = struct.unpack("i", data[:4])[0]
    rest = data[4:]
    end = rest.find(b"\0")
    rest = rest[end:].lstrip(b"\0") if end >= 0 else b""
    parts = rest.split(b"\0")
    argv = [p.decode("utf-8", "replace") for p in parts[:argc]]
    env = {}
    for p in parts[argc:]:
        if not p:
            break
        k, sep, v = p.decode("utf-8", "replace").partition("=")
        if sep:
            env[k] = v
    return argv, env


_static = {}  # pid -> {what: value} for things that don't change while a process lives


def _memo(pid, what, fn):
    slot = _static.setdefault(pid, {})
    if what not in slot:
        slot[what] = fn()
    return slot[what]


def forget_except(table):
    """Drop cached facts about processes that are gone (call once per process-table scan)."""
    for pid in list(_static):
        if pid not in table:
            del _static[pid]


def proc_args(pid):
    """(argv, env) of a process, exactly: `ps -o command` joins args with spaces and loses quoting."""
    return _memo(pid, "args", lambda: _proc_args(pid))


def _proc_args(pid):
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode("utf-8", "replace") for a in fh.read().split(b"\0")[:-1]]
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = dict(e.decode("utf-8", "replace").partition("=")[::2] for e in fh.read().split(b"\0") if b"=" in e)
            return argv, env
        except OSError:
            return [], {}
    if sys.platform == "darwin":
        argmax = _sysctl([1, 8], 4)  # CTL_KERN, KERN_ARGMAX
        size = struct.unpack("i", argmax)[0] if argmax else 1 << 20
        return parse_procargs2(_sysctl([1, 49, pid], size))  # KERN_PROCARGS2
    return [], {}


def proc_cwd(pid):
    return _memo(pid, "cwd", lambda: _proc_cwd(pid))


def _proc_cwd(pid):
    if sys.platform.startswith("linux"):
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    for line in run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"]).splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def proc_start(pid):
    """A process identity to compare before signalling, so a reused pid is never hit: start time in
    clock ticks on Linux; on macOS start time (to the second) plus the full command line."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat") as fh:
                return fh.read().rsplit(")", 1)[1].split()[19]  # field 22, starttime
        except (OSError, IndexError):
            return ""
    return run(["ps", "-o", "lstart=,command=", "-p", str(pid)]).strip()


def proc_start_epoch(pid):
    """Seconds since the epoch the process started, or None."""
    return _memo(pid, "start", lambda: _proc_start_epoch(pid))


def _proc_start_epoch(pid):
    out = run(["ps", "-o", "etimes=", "-p", str(pid)]).strip() if sys.platform.startswith("linux") else ""
    if out.isdigit():
        return time.time() - int(out)
    text = run(["ps", "-o", "lstart=", "-p", str(pid)]).strip()
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
