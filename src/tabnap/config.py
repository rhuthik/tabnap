"""Paths and settings. Settings come from ~/.config/tabnap/config.toml; every key is optional."""
import glob
import os
import tomllib
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")


def _xdg(var, default):
    return os.environ.get(var) or os.path.join(HOME, default)


STATE_DIR = os.environ.get("TABNAP_STATE_DIR") or os.path.join(_xdg("XDG_STATE_HOME", ".local/state"), "tabnap")
CONFIG_FILE = os.environ.get("TABNAP_CONFIG") or os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "tabnap", "config.toml")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
DAEMON_FILE = os.path.join(STATE_DIR, "daemon.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
LOG_FILE = os.path.join(STATE_DIR, "daemon.log")
ICON_PATH = os.path.join(STATE_DIR, "sleeping.png")

EXAMPLE = """\
# tabnap settings. Every key is optional; these are the defaults.

idle_minutes = 40        # auto-hibernate after this long idle (when auto is switched on)
bloat_mb = 1500          # flag a session for a refresh above this much memory
port = 47817             # local HTTP API and panel (127.0.0.1 only, token protected)
terminals = ["iterm", "tmux"]

[features]
context = false          # show your last prompt and the agent's last reply in the panel and `tabnap top`
needs_pulse = true       # blink a tab when its agent finishes or waits for you while you're elsewhere
title_spinner = true     # iTerm2: animate the titles of working tabs
tab_icon = true          # iTerm2: a sleeping icon on hibernated tabs

[claude]
# Extra Claude Code config dirs (globs ok). ~/.claude and each process's CLAUDE_CONFIG_DIR are always checked.
config_dirs = []
# Command used to resume. {id} = session id, {config_dir} = its config dir, {profile} = basename of it.
# A non-default config dir gets CLAUDE_CONFIG_DIR=... prepended unless the template mentions {config_dir} or {profile}.
resume = "claude --resume {id}"
# Per config dir overrides, e.g. shell wrappers that pick a profile:
# [claude.resume_by_dir]
# "~/.claude-profiles/*" = "claude-{profile} --resume {id}"

[codex]
home = "~/.codex"
resume = "codex resume {id}"

[tmux]
sockets = []             # extra tmux sockets (-L names or -S paths); the default server is always checked

[iterm]
window = ""              # limit tabnap to one iTerm window id (useful for trying it out)
"""


@dataclass
class Config:
    idle_minutes: float = 40
    bloat_mb: float = 1500
    port: int = 47817
    terminals: list = field(default_factory=lambda: ["iterm", "tmux"])
    claude_config_dirs: list = field(default_factory=list)
    claude_resume: str = "claude --resume {id}"
    claude_resume_by_dir: dict = field(default_factory=dict)
    codex_home: str = os.path.join(HOME, ".codex")
    codex_resume: str = "codex resume {id}"
    tmux_sockets: list = field(default_factory=list)
    iterm_window: str = ""
    context: bool = False
    needs_pulse: bool = True
    title_spinner: bool = True
    tab_icon: bool = True

    def claude_dirs(self):
        dirs = [os.path.join(HOME, ".claude")]
        for pattern in self.claude_config_dirs:
            dirs += sorted(glob.glob(os.path.expanduser(pattern)))
        return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def load(path=CONFIG_FILE):
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raw = {}
    c = Config()
    for key in ("idle_minutes", "bloat_mb", "port", "terminals"):
        if key in raw:
            setattr(c, key, raw[key])
    for key, value in raw.get("features", {}).items():
        if key in ("context", "needs_pulse", "title_spinner", "tab_icon"):
            setattr(c, key, bool(value))
    claude, codex = raw.get("claude", {}), raw.get("codex", {})
    c.claude_config_dirs = list(claude.get("config_dirs", []))
    c.claude_resume = claude.get("resume", c.claude_resume)
    c.claude_resume_by_dir = dict(claude.get("resume_by_dir", {}))
    c.codex_home = os.path.expanduser(codex.get("home", c.codex_home))
    c.codex_resume = codex.get("resume", c.codex_resume)
    c.tmux_sockets = list(raw.get("tmux", {}).get("sockets", []))
    c.iterm_window = str(raw.get("iterm", {}).get("window", ""))
    for env, key, cast in (("TABNAP_IDLE_MINUTES", "idle_minutes", float), ("TABNAP_PORT", "port", int),
                           ("TABNAP_BLOAT_MB", "bloat_mb", float), ("TABNAP_ITERM_WINDOW", "iterm_window", str)):
        if os.environ.get(env):
            setattr(c, key, cast(os.environ[env]))
    if os.environ.get("TABNAP_TERMINALS"):
        c.terminals = os.environ["TABNAP_TERMINALS"].split(",")
    if os.environ.get("TABNAP_TMUX_SOCKETS"):
        c.tmux_sockets = os.environ["TABNAP_TMUX_SOCKETS"].split(",")
    return c
