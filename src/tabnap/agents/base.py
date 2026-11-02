"""What an agent integration provides, and helpers they share."""
import os
import re
import shlex
from dataclasses import dataclass, field


@dataclass
class Live:
    """An agent process running in a terminal pane, and what we know about its session."""
    agent: "Agent"
    pid: int
    session_id: str | None = None
    transcript: str | None = None      # file the conversation lives in; needed to resume
    cwd: str | None = None
    status: str = "idle"               # working | waiting | idle
    status_at: float = 0.0             # epoch seconds of the last status change we can see
    resume: str | None = None          # shell command that brings this session back
    confident: bool = True             # False when the session was matched by a guess (auto-hibernate skips it)
    extra: dict = field(default_factory=dict)


class Agent:
    name = ""
    label = ""
    idle_glyph = ""        # what the agent puts in front of its tab title when idle
    sets_title = False     # whether the agent titles its own tab
    keep_flags = {}        # flag -> number of values (or "*" for until the next flag) carried over on resume
    quit_with = "signal"   # "signal": SIGINT the pid; "keys": type Ctrl-C into the pane

    def matches(self, argv):
        """argv (from `ps`, split on spaces) is an interactive session of this agent."""
        raise NotImplementedError

    def inspect(self, pid, cfg, claimed=frozenset()):
        """Live record for the agent process `pid`. `claimed`: session ids other panes already own."""
        raise NotImplementedError

    def resume_command(self, cfg, sid, argv=(), extra=None):
        """Shell command that resumes session `sid`, keeping the flags in keep_flags from argv."""
        raise NotImplementedError

    def title(self, live):
        return None

    def context(self, transcript):
        """{prompt, answer}: your last message and the agent's last reply."""
        return {}

    def loop_tool(self, transcript):
        return None

    def background(self, table, kids, pid):
        return False

    def input_blocker(self, lines):
        """Reason the screen says quitting now would lose something, or None."""
        return None

    def exit_session_id(self, lines):
        """Session id printed on exit, read from the screen."""
        return None

    def exit_line(self, lines):
        """Index of the line the agent printed on exit that the hibernate banner may overwrite."""
        return None


def kept_flags(argv, spec):
    """The subset of argv worth repeating on resume (model, permission mode...), quoted for a shell."""
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        name = a.split("=", 1)[0]
        if name in spec:
            if "=" in a or spec[name] == 0:
                out.append(a)
                i += 1
                continue
            if spec[name] == "*":
                j = i + 1
                while j < len(argv) and not argv[j].startswith("-"):
                    j += 1
                out += argv[i:j]
                i = j
                continue
            out += argv[i:i + 1 + spec[name]]
            i += 1 + spec[name]
            continue
        i += 1
    return " ".join(shlex.quote(a) for a in out)


def read_tail(path, tail_bytes):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - tail_bytes))
            return fh.read().decode("utf-8", "replace")
    except (OSError, TypeError):
        return ""


def one_line(text, limit=300):
    text = " ".join(re.sub(r"</?[a-z_-]+(?: [^>]*)?>", " ", text or "").split())  # <pasted_content id=..> wrappers
    return text[:limit]


def basename(path):
    return os.path.basename(path or "")
