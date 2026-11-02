"""What a terminal integration provides."""
from dataclasses import dataclass, field

SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh", "tcsh", "nu", "login", "xonsh", "elvish"}
PLACEHOLDER_TITLES = {"", "Chat", "Default", "claude", "Claude Code", "codex", "node"}


def is_shell(command):
    return (command or "").lstrip("-").split("/")[-1] in SHELLS


@dataclass
class Pane:
    key: str                 # globally unique: "<terminal>:<id>"
    tty: str | None
    name: str                # the tab / pane title as the terminal shows it
    focused: bool
    shell: bool              # a shell is in the foreground (a prompt is showing)
    order: tuple
    term: "Terminal" = field(repr=False, default=None)
    ref: object = field(repr=False, default=None)


@dataclass
class Look:
    """How the engine wants a pane to look. Adapters apply what they can and skip the rest."""
    state: str               # working | waiting | idle | hibernated | waking
    title: str = ""
    agent: str = ""
    idle_glyph: str = ""
    sets_title: bool = True
    status_text: str = ""    # the line shown for a hibernated tab ("hibernated 2h · 340 MB freed")
    bar: str = ""            # the clickable status bar text ("✳ Idle 12m · Hibernate")


class Terminal:
    kind = ""
    has_panel = False        # can host the web panel in its own UI

    @property
    def prefix(self):
        return self.kind

    def __init__(self, cfg):
        self.cfg = cfg
        self.up = False
        self.keys = {}       # pane key -> last keystroke time, if the terminal can tell us

    async def start(self, engine):
        """Connect. Return False if this terminal isn't running or usable here."""
        return False

    async def panes(self):
        return []

    async def screen(self, pane, drop_faint=False):
        """(lines, cursor_row) of the visible screen. drop_faint: leave out dim text, which is what
        agents use for placeholders and suggested prompts (so they don't look like a typed draft)."""
        return [], 0

    async def shell_ready(self, pane):
        return False

    async def send(self, pane, text):
        """Type text into the pane. \\x03 (Ctrl-C), \\x15 (Ctrl-U) and \\r (Enter) are keys."""

    async def apply(self, pane, look):
        """Decorate the pane for `look`, or take our decorations off when look is None."""

    async def pulse(self, pane, on):
        """Light up (or not) a tab that needs you."""

    async def focus(self, pane):
        pass

    async def agent_status(self, pane):
        """The terminal's own idea of the agent's state, if it has one ({status, detail})."""
        return {}

    async def run(self, engine):
        """Background work of this terminal (keystroke monitor, title spinner); runs while up."""

    async def revert_all(self):
        pass
