"""tmux: works inside any terminal emulator, on macOS and Linux.

Labels go into window options (@tabnap_label, @tabnap_title) that the status line reads; run
`tabnap setup tmux` for the format lines and a popup key for `tabnap top`.
"""
import asyncio
import re
import shutil

from tabnap.terminals.base import Pane, Terminal, is_shell

FIELDS = ["pane_id", "pane_tty", "pane_current_command", "pane_active", "window_active",
          "session_attached", "window_id", "session_name", "window_index", "pane_index", "pane_title"]
KEYS = {"\x03": "C-c", "\x15": "C-u", "\r": "Enter"}
LABELS = {"working": "● ", "waiting": "! ", "hibernated": "zz ", "waking": "… "}
PULSE_STYLE = "bg=colour172,fg=colour232"

SNIPPET = """\
# tabnap: a state label (● working, ! waiting, zz hibernated) before each window name,
# and the session title while hibernated. Adjust to taste.
set -g window-status-format         "#I:#{@tabnap_label}#{?@tabnap_title,#{=30:@tabnap_title},#W}#F"
set -g window-status-current-format "#I:#{@tabnap_label}#{?@tabnap_title,#{=30:@tabnap_title},#W}#F"
# prefix + N: the tabnap session list in a popup
bind-key N display-popup -E -w 90% -h 80% "{tabnap} top"
"""


ESCAPE = re.compile(r"\x1b\[[0-9;:?]*[A-Za-z]|\x1b[^\[]")


def strip_faint(line):
    """Text of a `capture-pane -e` line without the cells drawn dim (SGR 2), and without escapes."""
    out, dim, pos = [], False, 0
    for m in ESCAPE.finditer(line):
        if not dim:
            out.append(line[pos:m.start()])
        pos = m.end()
        seq = m.group()
        if not (seq.startswith("\x1b[") and seq.endswith("m")):
            continue
        codes = [c or "0" for c in seq[2:-1].replace(":", ";").split(";")]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c in ("38", "48", "58"):  # extended color: 38;5;n or 38;2;r;g;b, whose numbers aren't codes
                i += 3 if i + 1 < len(codes) and codes[i + 1] == "5" else 5
                continue
            if c == "0" or c == "22":
                dim = False
            elif c == "2":
                dim = True
            i += 1
    if not dim:
        out.append(line[pos:])
    return "".join(out)


def parse_panes(out):
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == len(FIELDS):
            rows.append(dict(zip(FIELDS, parts)))
    return rows


class Tmux(Terminal):
    kind = "tmux"

    def __init__(self, cfg, socket=None):
        super().__init__(cfg)
        self.socket = socket
        self.applied = {}    # pane key -> {option: value} already set

    def base(self):
        if not self.socket:
            return ["tmux"]
        return ["tmux", "-S" if "/" in self.socket else "-L", self.socket]

    async def tmux(self, *args):
        proc = await asyncio.create_subprocess_exec(*self.base(), *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return proc.returncode, out.decode("utf-8", "replace")

    async def start(self, engine):
        if not shutil.which("tmux"):
            return False
        code, _ = await self.tmux("list-sessions")
        return code == 0

    @property
    def prefix(self):
        return f"tmux{'-' + re.sub(r'[^A-Za-z0-9_.-]', '_', self.socket) if self.socket else ''}"

    async def panes(self):
        code, out = await self.tmux("list-panes", "-a", "-F", "\t".join("#{%s}" % f for f in FIELDS))
        if code != 0:
            raise ConnectionError("tmux server not reachable")
        panes = []
        for r in parse_panes(out):
            focused = r["pane_active"] == "1" and r["window_active"] == "1" and r["session_attached"] not in ("", "0")
            panes.append(Pane(
                key=f"{self.prefix}:{r['pane_id']}", tty=r["pane_tty"], name=r["pane_title"], focused=focused,
                shell=is_shell(r["pane_current_command"]),
                order=(r["session_name"], int(r["window_index"] or 0), int(r["pane_index"] or 0)),
                term=self, ref=r))
        return panes

    async def screen(self, pane, drop_faint=False):
        flags = ["-e"] if drop_faint else []
        _, out = await self.tmux("capture-pane", "-p", *flags, "-t", pane.ref["pane_id"])
        _, cur = await self.tmux("display-message", "-p", "-t", pane.ref["pane_id"], "#{cursor_y}")
        lines = [strip_faint(l) for l in out.split("\n")] if drop_faint else out.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines, int(cur.strip() or 0)

    async def shell_ready(self, pane):
        _, out = await self.tmux("display-message", "-p", "-t", pane.ref["pane_id"], "#{pane_current_command}")
        return is_shell(out.strip())

    async def send(self, pane, text):
        target = pane.ref["pane_id"]
        for chunk in re.split(r"([\x03\x15\r])", text):
            if not chunk:
                continue
            if chunk in KEYS:
                await self.tmux("send-keys", "-t", target, KEYS[chunk])
            else:
                await self.tmux("send-keys", "-t", target, "-l", chunk)

    async def set_opt(self, pane, name, value):
        seen = self.applied.setdefault(pane.key, {})
        if seen.get(name) == value:
            return
        if value:
            await self.tmux("set-option", "-w", "-t", pane.ref["pane_id"], name, value)
        else:
            await self.tmux("set-option", "-uw", "-t", pane.ref["pane_id"], name)
        seen[name] = value

    async def apply(self, pane, look):
        if look is None:
            if self.applied.get(pane.key):
                for name in ("@tabnap_label", "@tabnap_title"):
                    await self.set_opt(pane, name, "")
            return
        await self.set_opt(pane, "@tabnap_label", LABELS.get(look.state, ""))
        await self.set_opt(pane, "@tabnap_title", look.title if look.state in ("hibernated", "waking") else "")

    async def pulse(self, pane, on):
        await self.set_opt(pane, "window-status-style", PULSE_STYLE if on else "")

    async def focus(self, pane):
        t = pane.ref["pane_id"]
        await self.tmux("switch-client", "-t", t)
        await self.tmux("select-window", "-t", t)
        await self.tmux("select-pane", "-t", t)

    async def revert_all(self):
        for key in list(self.applied):
            pane_id = key.rsplit(":", 1)[1]
            for name, value in self.applied[key].items():
                if value:
                    await self.tmux("set-option", "-uw", "-t", pane_id, name)
        self.applied.clear()
