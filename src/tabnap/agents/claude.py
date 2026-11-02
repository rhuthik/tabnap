"""Claude Code: session files in <config>/sessions/<pid>.json, transcripts in <config>/projects/*/<id>.jsonl."""
import fnmatch
import glob
import json
import os
import re
import shlex

from tabnap import procs
from tabnap.agents.base import Agent, Live, basename, kept_flags, one_line, read_tail
from tabnap.config import HOME

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
LOOP_TOOLS = ("ScheduleWakeup", "CronCreate")
SKIP_PROMPTS = ("<command-", "<local-command", "<system-reminder", "<bash-",
                "This session is being continued", "Caveat:")


def read_session_file(path):
    # Usually one JSON object, but has been seen with trailing JSONL; take the first object.
    try:
        with open(path) as fh:
            return json.JSONDecoder().raw_decode(fh.read().strip())[0]
    except (OSError, ValueError):
        return None


def resume_arg(argv):
    for i, a in enumerate(argv):
        if a in ("--resume", "-r", "--session-id") and i + 1 < len(argv):
            a = argv[i + 1]
        elif a.startswith(("--resume=", "--session-id=")):
            a = a.split("=", 1)[1]
        else:
            continue
        if re.fullmatch(UUID, a):
            return a
    return None


def transcript_title(transcript):
    """The session's title as Claude shows it: the latest /rename, else its generated title."""
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except (OSError, TypeError):
        return None
    for key in ("customTitle", "aiTitle"):
        found = re.findall(r'"' + key + r'":"((?:[^"\\]|\\.)*)"', data)
        if found:
            try:
                return json.loads(f'"{found[-1]}"').strip() or None
            except ValueError:
                return None
    return None


def last_context(transcript, tail_bytes=512 * 1024):
    """{prompt, answer}: the last thing you typed and Claude's last reply to it."""
    lines = read_tail(transcript, tail_bytes).splitlines()
    ctx = {}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # first line of the tail is usually cut mid-entry
        if entry.get("type") == "assistant" and "answer" not in ctx:
            blocks = entry.get("message", {}).get("content")
            text = " ".join(b.get("text", "") for b in blocks if b.get("type") == "text") if isinstance(blocks, list) else ""
            if text.strip():
                ctx["answer"] = " ".join(text.split())[:300]
            continue
        if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isCompactSummary"):
            continue
        content = entry.get("message", {}).get("content")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        text = " ".join((content or "").split())
        if not text or text.startswith(SKIP_PROMPTS):
            continue
        text = one_line(text)
        if text:
            ctx["prompt"] = text
            break
    return ctx


def recent_loop_tool(transcript, tail_bytes=256 * 1024):
    data = read_tail(transcript, tail_bytes)
    # Match calls only: tool definitions ({"name":"ScheduleWakeup","description":...}) are in the transcript too.
    m = re.search(r'"type":"tool_use","id":"[^"]*","name":"(' + "|".join(LOOP_TOOLS) + ')"', data)
    return m.group(1) if m else None


class Claude(Agent):
    name = "claude"
    label = "Claude"
    idle_glyph = "✳"
    sets_title = True
    quit_with = "signal"  # Claude exits cleanly on SIGINT in ~1s and takes its MCP servers with it
    keep_flags = {
        "--model": 1, "--permission-mode": 1, "--dangerously-skip-permissions": 0,
        "--allow-dangerously-skip-permissions": 0, "--add-dir": "*", "--mcp-config": "*",
        "--strict-mcp-config": 0, "--settings": 1, "--agent": 1, "--fallback-model": 1, "--effort": 1,
        "--chrome": 0, "--no-chrome": 0, "--ide": 0, "--plugin-dir": "*",
        "--append-system-prompt": 1, "--system-prompt": 1, "--setting-sources": 1,
    }

    def matches(self, argv):
        if not argv or basename(argv[0]) not in ("claude", "claude.exe"):
            return False
        if len(argv) > 1 and argv[1] in ("mcp", "update", "doctor", "config", "install", "plugin", "setup-token"):
            return False
        return not {"--chrome-native-host", "-p", "--print"} & set(argv[1:])

    def inspect(self, pid, cfg, claimed=frozenset()):
        argv, env = procs.proc_args(pid)
        dirs = ([env["CLAUDE_CONFIG_DIR"]] if env.get("CLAUDE_CONFIG_DIR") else []) + cfg.claude_dirs()
        meta, config_dir = None, None
        for d in dict.fromkeys(os.path.expanduser(x) for x in dirs):
            meta = read_session_file(os.path.join(d, "sessions", f"{pid}.json"))
            if meta and meta.get("sessionId"):
                config_dir = d
                break
            meta = None
        if not meta:
            # No session file yet (startup dialog) or one we can't find: resumable only if argv names it.
            sid = resume_arg(argv)
            live = Live(self, pid, session_id=sid, status="idle", confident=False,
                        extra={"config_dir": env.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"), "argv": argv})
            if sid:
                live.resume = self.resume_command(cfg, sid, argv, live.extra)
            return live
        sid = meta["sessionId"]
        hits = glob.glob(os.path.join(config_dir, "projects", "*", f"{sid}.jsonl"))
        raw = meta.get("status") or "idle"
        status = {"busy": "working", "idle": "idle"}.get(raw, "waiting" if "wait" in raw else "working")
        live = Live(self, pid, session_id=sid, transcript=hits[0] if hits else None, cwd=meta.get("cwd"),
                    status=status, status_at=(meta.get("statusUpdatedAt") or 0) / 1000,
                    extra={"config_dir": config_dir, "argv": argv})
        live.resume = self.resume_command(cfg, sid, argv, live.extra)
        return live

    def resume_command(self, cfg, sid, argv=(), extra=None):
        config_dir = os.path.expanduser((extra or {}).get("config_dir") or os.path.join(HOME, ".claude"))
        template, explicit = cfg.claude_resume, False
        for pattern, t in cfg.claude_resume_by_dir.items():
            if fnmatch.fnmatch(config_dir, os.path.expanduser(pattern)):
                template, explicit = t, True  # you said how this dir resumes (e.g. a wrapper that sets it)
                break
        cmd = template.format(id=sid, config_dir=shlex.quote(config_dir), profile=basename(config_dir))
        default = os.path.join(HOME, ".claude")
        if not explicit and os.path.normpath(config_dir) != default and "{config_dir}" not in template:
            cmd = f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} {cmd}"
        flags = kept_flags(list(argv[1:]), self.keep_flags)
        return f"{cmd} {flags}".strip()

    def title(self, live):
        return transcript_title(live.transcript) if live.transcript else None

    def context(self, transcript):
        ctx = last_context(transcript)
        if "prompt" not in ctx:  # long tool output since the last prompt; look further back once
            ctx = last_context(transcript, tail_bytes=4 * 1024 * 1024)
        return ctx

    def loop_tool(self, transcript):
        return recent_loop_tool(transcript)

    def background(self, table, kids, pid):
        """A Bash-tool shell (foreground or run_in_background) still running under this claude."""
        return any("/shell-snapshots/snapshot-" in table.get(d, (0, "", ""))[2] for d in procs.descendants(kids, pid))

    def input_blocker(self, lines):
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].startswith("❯") and lines[i - 1].startswith("──"):
                return "draft in input box" if lines[i][1:].strip() else None
        return "dialog or menu open"

    def exit_session_id(self, lines):
        for line in reversed(lines):
            m = re.search(r"--resume (" + UUID + ")", line)
            if m:
                return m.group(1)
        return None

    def exit_line(self, lines):
        return max((i for i, l in enumerate(lines) if l.startswith("Resume this session with")), default=None)
