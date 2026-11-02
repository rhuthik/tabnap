"""Paths and settings. Settings live in ~/.config/tabnap/config.toml; every key is optional.

SETTINGS is the single list of settings: loading, `tabnap config`, `tabnap config set`, the panel's
settings view and the file tabnap writes all come from it.
"""
import fnmatch
import glob
import json
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

AGENT_NAMES = ("claude", "codex")


@dataclass(frozen=True)
class Setting:
    key: str          # dotted TOML path
    attr: str         # Config attribute
    kind: str         # float | int | str | bool | list | path | dict
    help: str
    restart: bool = False   # takes effect after `tabnap restart` (everything else applies live)


SETTINGS = [
    Setting("idle_minutes", "idle_minutes", "float", "auto-hibernate a session after this many idle minutes"),
    Setting("bloat_mb", "bloat_mb", "float", "suggest a refresh when a session uses more than this many MB"),
    Setting("panel_title", "panel_title", "str", "heading of the session list (iTerm2 Toolbelt, browser panel, tabnap top)"),
    Setting("port", "port", "int", "local API and panel port (127.0.0.1 only)", restart=True),
    Setting("terminals", "terminals", "list", "terminals to watch: iterm, tmux", restart=True),
    Setting("auto.max_awake", "max_awake", "int",
            "when auto is on, keep at most this many sessions awake; the longest-idle ones sleep first (0 = no limit)"),
    Setting("auto.agents", "auto_agents", "list", "agents auto-hibernate may put to sleep: claude, codex"),
    Setting("auto.skip_dirs", "skip_dirs", "list", "never auto-hibernate sessions in these folders (globs ok; manual still works)"),
    Setting("features.context", "context", "bool", "show your last prompt and the agent's last reply in the session list"),
    Setting("features.needs_pulse", "needs_pulse", "bool", "blink a tab when its agent finishes or waits for you"),
    Setting("features.title_spinner", "title_spinner", "bool", "iTerm2: animate the titles of working tabs"),
    Setting("features.tab_icon", "tab_icon", "bool", "iTerm2: a sleeping icon on hibernated tabs"),
    Setting("claude.config_dirs", "claude_config_dirs", "list",
            "extra Claude Code config dirs (globs ok); ~/.claude and each process's CLAUDE_CONFIG_DIR are always checked"),
    Setting("claude.resume", "claude_resume", "str",
            "resume command; {id} session id, {config_dir}, {profile} = basename of the config dir"),
    Setting("claude.resume_by_dir", "claude_resume_by_dir", "dict",
            "per config dir resume commands, e.g. \"~/.claude-profiles/*\" = \"claude-{profile} --resume {id}\""),
    Setting("codex.home", "codex_home", "path", "Codex home (CODEX_HOME)"),
    Setting("codex.resume", "codex_resume", "str", "resume command; {id} = thread id"),
    Setting("tmux.sockets", "tmux_sockets", "list", "extra tmux sockets (-L names or -S paths); the default server is always checked",
            restart=True),
    Setting("iterm.window", "iterm_window", "str", "limit tabnap to one iTerm2 window id (for trying it out)", restart=True),
    Setting("iterm.icon", "iterm_icon", "path", "PNG for hibernated tabs (empty: a sleeping moon)", restart=True),
    Setting("snapshot.dir", "snapshot_dir", "path", "write every session + its resume command here for crash recovery (empty: off)",
            restart=True),
    Setting("snapshot.name", "snapshot_name", "str", "snapshot file name (strftime)"),
    Setting("snapshot.every_minutes", "snapshot_minutes", "float", "minutes between snapshots"),
    Setting("snapshot.keep", "snapshot_keep", "int", "snapshots to keep"),
]
BY_KEY = {s.key: s for s in SETTINGS}


@dataclass
class Config:
    idle_minutes: float = 40
    bloat_mb: float = 1500
    panel_title: str = "Agent sessions"
    port: int = 47817
    terminals: list = field(default_factory=lambda: ["iterm", "tmux"])
    max_awake: int = 0
    auto_agents: list = field(default_factory=lambda: list(AGENT_NAMES))
    skip_dirs: list = field(default_factory=list)
    context: bool = False
    needs_pulse: bool = True
    title_spinner: bool = True
    tab_icon: bool = True
    claude_config_dirs: list = field(default_factory=list)
    claude_resume: str = "claude --resume {id}"
    claude_resume_by_dir: dict = field(default_factory=dict)
    codex_home: str = os.path.join(HOME, ".codex")
    codex_resume: str = "codex resume {id}"
    tmux_sockets: list = field(default_factory=list)
    iterm_window: str = ""
    iterm_icon: str = ""
    snapshot_dir: str = ""
    snapshot_name: str = "tabnap-%Y%m%d-%H%M.json"
    snapshot_minutes: float = 15
    snapshot_keep: int = 3

    def claude_dirs(self):
        dirs = [os.path.join(HOME, ".claude")]
        for pattern in self.claude_config_dirs:
            dirs += sorted(glob.glob(os.path.expanduser(pattern)))
        return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]

    def skipped_dir(self, cwd):
        """True when auto-hibernate should leave sessions in `cwd` alone (the folder or anything under it)."""
        for pattern in self.skip_dirs:
            p = os.path.expanduser(pattern).rstrip("/")
            if cwd and (fnmatch.fnmatch(cwd, p) or fnmatch.fnmatch(cwd, p + "/*")):
                return True
        return False


def coerce(setting, value):
    """Turn a TOML value or a command-line string into the setting's type; ValueError if it doesn't fit."""
    kind = setting.kind
    if kind == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{setting.key}: expected on/off")
    if kind in ("float", "int"):
        n = (float if kind == "float" else int)(value)
        if n < 0:
            raise ValueError(f"{setting.key}: must not be negative")
        if setting.key == "idle_minutes" and n <= 0:
            raise ValueError("idle_minutes: must be more than 0")
        return n
    if kind == "list":
        if isinstance(value, str):
            text = value.strip()
            value = json.loads(text) if text.startswith("[") else [v.strip() for v in text.split(",") if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{setting.key}: expected a list of strings")
        if setting.key == "auto.agents" and not set(value) <= set(AGENT_NAMES):
            raise ValueError(f"auto.agents: only {', '.join(AGENT_NAMES)}")
        return value
    if kind == "dict":
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            raise ValueError(f"{setting.key}: expected a table / JSON object")
        return {str(k): str(v) for k, v in value.items()}
    if kind == "path":
        return os.path.expanduser(str(value)) if value else ""
    return str(value)


def _dig(raw, key):
    node = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def load(path=None):
    path = path or CONFIG_FILE
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raw = {}
    c = Config()
    for s in SETTINGS:
        value, found = _dig(raw, s.key)
        if found:
            try:
                setattr(c, s.attr, coerce(s, value))
            except (ValueError, TypeError):
                pass  # a bad value keeps the default rather than stopping the daemon
    for env, key, cast in (("TABNAP_IDLE_MINUTES", "idle_minutes", float), ("TABNAP_PORT", "port", int),
                           ("TABNAP_BLOAT_MB", "bloat_mb", float), ("TABNAP_ITERM_WINDOW", "iterm_window", str)):
        if os.environ.get(env):
            setattr(c, key, cast(os.environ[env]))
    if os.environ.get("TABNAP_TERMINALS"):
        c.terminals = os.environ["TABNAP_TERMINALS"].split(",")
    if os.environ.get("TABNAP_TMUX_SOCKETS"):
        c.tmux_sockets = os.environ["TABNAP_TMUX_SOCKETS"].split(",")
    return c


def toml_value(v):
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return f"{v:g}" if isinstance(v, float) else str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, str):
        if v.startswith(HOME + "/"):
            v = "~" + v[len(HOME):]
        return json.dumps(v, ensure_ascii=False)
    raise TypeError(type(v))


def render(c):
    """The whole config file for `c`, every setting with its help line."""
    lines = ["# tabnap settings. Change them with `tabnap config set <key> <value>` (applied live),",
             "# from the panel's gear, or by editing this file and running `tabnap restart`.", ""]
    section = ""
    for s in SETTINGS:
        sec, _, name = s.key.rpartition(".")
        value = getattr(c, s.attr)
        if s.kind == "dict":
            lines += ["", f"# {s.help}", f"[{s.key}]"]
            lines += [f"{json.dumps(k)} = {toml_value(v)}" for k, v in value.items()]
            section = s.key
            continue
        if sec != section:
            lines += ["", f"[{sec}]"]
            section = sec
        lines.append(f"{name} = {toml_value(value)}  # {s.help}{' (restart)' if s.restart else ''}")
    return "\n".join(lines).lstrip("\n").replace("\n\n\n", "\n\n") + "\n"


def write(c, path=None):
    path = path or CONFIG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as fh:
        fh.write(render(c))
    os.replace(path + ".tmp", path)


def set_value(key, value, path=None):
    """Validate and save one setting. Returns (new Config, Setting)."""
    if key not in BY_KEY:
        raise KeyError(f"unknown setting {key!r}; see `tabnap config`")
    s = BY_KEY[key]
    c = load(path)
    setattr(c, s.attr, coerce(s, value))
    write(c, path)
    return c, s


def describe(c):
    """[(key, value, help, restart)] for listings."""
    return [(s.key, getattr(c, s.attr), s.help, s.restart) for s in SETTINGS]


EXAMPLE = render(Config())
