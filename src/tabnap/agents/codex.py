"""OpenAI Codex CLI: threads in <CODEX_HOME>/state_*.sqlite, conversations in sessions/**/rollout-*.jsonl.

Recent Codex versions run a shared `codex app-server` daemon that owns the thread; the TUI in the tab
is a client. Nothing on disk links a TUI pid to its thread, so it's matched by cwd and start time, and
on hibernate the id is read from the `codex resume <id>` line Codex prints when it quits.
"""
import glob
import json
import os
import re
import sqlite3

from tabnap import procs
from tabnap.agents.base import Agent, Live, basename, kept_flags, one_line, read_tail

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
NOT_INTERACTIVE = {"exec", "e", "app-server", "mcp", "mcp-server", "login", "logout", "apply", "a",
                   "completion", "sandbox", "debug", "cloud", "proto", "help", "features", "review", "agents"}
PLACEHOLDERS = {"", "Ask Codex to do anything", "Ask a follow-up question"}
SLACK_MS = 5000


def codex_argv(argv):
    """argv without the launcher: `node .../bin/codex -m x` -> ['codex', '-m', 'x']."""
    if argv and basename(argv[0]) in ("node", "bun") and len(argv) > 1 and basename(argv[1]) == "codex":
        return argv[1:]
    return argv


def rollout_status(rollout, tail_bytes=256 * 1024):
    """(status, last_event_time) from the rollout tail: a turn in flight is working, a pending approval is waiting."""
    status = None
    for line in reversed(read_tail(rollout, tail_bytes).splitlines()):
        if '"event_msg"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        kind = payload.get("type", "")
        if kind.endswith("approval_request") and status is None:
            status = "waiting"
        elif kind in ("task_complete", "turn_aborted"):
            return status or "idle"
        elif kind == "task_started":
            return status or "working"
    return status or "idle"


def rollout_context(rollout, tail_bytes=512 * 1024):
    """{prompt, answer} from the rollout: user/assistant messages, minus injected context blocks."""
    ctx = {}
    for line in reversed(read_tail(rollout, tail_bytes).splitlines()):
        if '"response_item"' not in line:
            continue
        try:
            p = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        if p.get("type") != "message":
            continue
        text = " ".join(c.get("text", "") for c in p.get("content") or [] if isinstance(c, dict))
        if p.get("role") == "assistant" and "answer" not in ctx and text.strip():
            ctx["answer"] = " ".join(text.split())[:300]
        elif p.get("role") == "user":
            stripped = text.strip()
            if not stripped or stripped.startswith(("<", "# AGENTS.md")):
                continue  # <environment_context>, <user_instructions>, AGENTS.md: added by Codex, not typed
            ctx["prompt"] = one_line(stripped)
            break
    return ctx


def pick_thread(rows, start_ms, claimed):
    """rows: (id, rollout, title, created_ms, updated_ms). Prefer a thread created after the TUI started
    (a new session); else one updated after it (resumed from the picker). (row, confident)."""
    rows = [r for r in rows if r[0] not in claimed]
    fresh = sorted((r for r in rows if r[3] >= start_ms - SLACK_MS), key=lambda r: abs(r[3] - start_ms))
    if fresh:
        return fresh[0], len(fresh) == 1
    touched = sorted((r for r in rows if r[4] >= start_ms - SLACK_MS), key=lambda r: -r[4])
    if touched:
        return touched[0], len(touched) == 1
    return None, False


class Codex(Agent):
    name = "codex"
    label = "Codex"
    sets_title = True   # Codex titles its tab "<thread> | <dir>"
    quit_with = "keys"  # a raw-mode TUI: Ctrl-C as a key exits cleanly, a SIGINT signal could skip terminal cleanup
    keep_flags = {
        "-m": 1, "--model": 1, "-p": 1, "--profile": 1, "-s": 1, "--sandbox": 1, "-a": 1,
        "--ask-for-approval": 1, "--full-auto": 0, "--dangerously-bypass-approvals-and-sandbox": 0,
        "--yolo": 0, "--search": 0, "-c": 1, "--config": 1, "--add-dir": 1, "--enable": 1, "--disable": 1,
        "--oss": 0, "--local-provider": 1,
    }

    def matches(self, argv):
        argv = codex_argv(argv)
        if not argv or basename(argv[0]) != "codex":
            return False
        return len(argv) == 1 or argv[1] not in NOT_INTERACTIVE

    def home(self, cfg, env):
        return os.path.expanduser(env.get("CODEX_HOME") or cfg.codex_home)

    def threads(self, home, cwd=None, thread_id=None):
        dbs = sorted(glob.glob(os.path.join(home, "state_*.sqlite")),
                     key=lambda p: int(re.sub(r"\D", "", basename(p)) or 0))
        if not dbs:
            return None
        try:
            con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True, timeout=1)
            try:
                cols = {r[1] for r in con.execute("pragma table_info(threads)")}
                sub = " and agent_role is null" if "agent_role" in cols else ""
                name = "coalesce(nullif(name, ''), title)" if "name" in cols else "title"  # name: generated or /rename
                q = (f"select id, rollout_path, {name}, created_at_ms, updated_at_ms from threads "
                     "where archived = 0 and source != 'exec'" + sub)
                if thread_id:
                    return con.execute(q + " and id = ?", (thread_id,)).fetchall()
                return con.execute(q + " and cwd = ?", (cwd,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return None

    def scan_rollouts(self, home, cwd, since):
        """Fallback without the state DB: rollouts written since the TUI started, in its cwd."""
        rows = []
        for path in glob.glob(os.path.join(home, "sessions", "*", "*", "*", "rollout-*.jsonl")):
            try:
                mtime = os.path.getmtime(path)
                if mtime < since - SLACK_MS / 1000:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    meta = (json.loads(fh.readline()) or {}).get("payload") or {}
            except (OSError, ValueError):
                continue
            if meta.get("cwd") == cwd and meta.get("id"):
                created = os.path.getctime(path)
                rows.append((meta["id"], path, None, int(created * 1000), int(mtime * 1000)))
        return rows

    def inspect(self, pid, cfg, claimed=frozenset()):
        argv, env = procs.proc_args(pid)
        argv = codex_argv(argv)
        home = self.home(cfg, env)
        cwd = procs.proc_cwd(pid)
        start = procs.proc_start_epoch(pid) or 0
        wanted = next((a for a in argv if re.fullmatch(UUID, a)), None) if "resume" in argv else None
        rows = self.threads(home, thread_id=wanted) if wanted else self.threads(home, cwd=cwd)
        if rows is None:
            rows = self.scan_rollouts(home, cwd, start)
        row, confident = (rows[0], True) if wanted and rows else pick_thread(rows, start * 1000, claimed)
        live = Live(self, pid, cwd=cwd, status="idle", confident=confident, extra={"home": home, "argv": argv})
        if not row:
            return live  # no prompt sent yet: no thread to come back to
        live.session_id, live.transcript = row[0], row[1]
        live.extra["title"] = row[2]
        live.status = rollout_status(row[1])
        try:
            live.status_at = os.path.getmtime(row[1])
        except OSError:
            pass
        live.resume = self.resume_command(cfg, row[0], argv, live.extra)
        return live

    def resume_command(self, cfg, sid, argv=(), extra=None):
        flags = kept_flags(list(argv[1:]), self.keep_flags)
        base = cfg.codex_resume.format(id=sid) if sid else " ".join(cfg.codex_resume.replace("{id}", "").split())
        return f"{base} {flags}".strip()  # no id: `codex resume` opens its picker

    def title(self, live):
        t = (live.extra.get("title") or "").strip()
        return one_line(t, 80) if t else None

    def context(self, transcript):
        ctx = rollout_context(transcript)
        if "prompt" not in ctx:
            ctx = rollout_context(transcript, tail_bytes=4 * 1024 * 1024)
        return ctx

    def input_blocker(self, lines):
        tail = [l for l in lines if l.strip()][-8:]
        for line in reversed(tail):
            if line.startswith("›"):
                return None if line[1:].strip() in PLACEHOLDERS else "draft in input box"
        return "dialog or menu open"

    def exit_session_id(self, lines):
        for line in reversed(lines):
            m = re.search(r"codex resume (" + UUID + ")", line)
            if m:
                return m.group(1)
        return None

    def exit_line(self, lines):
        return max((i for i, l in enumerate(lines) if re.search(r"codex resume " + UUID, l)), default=None)
