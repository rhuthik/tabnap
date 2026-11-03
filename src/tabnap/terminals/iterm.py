"""iTerm2, through its Python API (Settings > General > Magic > Enable Python API).

Hibernated tabs get a "zz <title>" name, a sleeping icon and a purple status line; working tabs get a
spinner; tabs that need you blink. A Toolbelt panel (View > Toolbelt > Agent sessions) lists every session,
and `tabnap setup iterm` adds a Wake/Hibernate button to the status bar.
"""
import asyncio
import json
import os
import struct
import subprocess
import time
import zlib

import iterm2

from tabnap.config import ICON_PATH
from tabnap.engine import log, strip_glyph
from tabnap.terminals.base import PLACEHOLDER_TITLES, Pane, Terminal, is_shell

TOGGLE_INVOCATION = "tabnap_toggle(session_id: id)"
BAR_VAR = "user.tabnap_bar"
TOOL_ID = "io.github.tabnap.panel"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SPIN_SECONDS = 0.3
PURPLE = "#a89de0"
PULSE_RGB = (196, 132, 40)


def iterm_running():
    comms = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout.splitlines()
    return any(c.endswith("/iTerm.app/Contents/MacOS/iTerm2") for c in comms)


def unfaint(line):
    """A screen line without its faint (dim) cells."""
    out, x = [], 0
    while True:
        try:
            ch = line.string_at(x)
        except IndexError:
            break
        style = line.style_at(x)
        if not (style and style.faint):
            out.append(ch)
        x += 1
    return "".join(out)


class ITerm(Terminal):
    kind = "iterm"
    has_panel = True

    def __init__(self, cfg):
        super().__init__(cfg)
        self.conn = None
        self.app = None
        self.applied = {}    # session id -> {what: value} already pushed to iTerm
        self.my_names = {}   # session id -> tab name we set last (spinner / zz)
        self.ttys = {}       # session id -> tty
        self.looks = {}      # session id -> last Look, for the spinner
        self.base_props = {} # (profile guid, key) -> value in the tab's own profile, to restore on wake
        self.frame = 0
        self.icon = ICON_PATH

    # --- connection ---

    async def start(self, engine):
        if not iterm_running():
            return False  # don't let the API's AppleScript cookie request launch iTerm
        self.engine = engine
        self.conn = await iterm2.Connection.async_create()
        self.app = await iterm2.async_get_app(self.conn)
        self.icon = self.cfg.iterm_icon if self.cfg.iterm_icon and os.path.exists(self.cfg.iterm_icon) else ICON_PATH
        write_icon(ICON_PATH)
        await self.adopt_existing_decorations()

        # The status bar button is a built-in "Custom Action" component (see `tabnap setup iterm`): iTerm
        # 3.7 doesn't list script-registered status bar components in its palette, but a Custom Action
        # can call this function, and its title can interpolate \(user.tabnap_bar).
        @iterm2.RPC
        async def tabnap_toggle(session_id=iterm2.Reference("id")):
            try:
                await engine.toggle(f"{self.prefix}:{session_id}")
            except Exception as e:  # pylint: disable=broad-except
                log(f"status bar click: {e}")

        await tabnap_toggle.async_register(self.conn)
        await iterm2.tool.async_register_web_view_tool(
            self.conn, self.cfg.panel_title, TOOL_ID, False, f"http://127.0.0.1:{engine.port}/panel?t={engine.token}")
        return True

    async def on_config(self, cfg, changed_keys):
        if "panel_title" in changed_keys:  # same tool id, new name in View > Toolbelt
            await iterm2.tool.async_register_web_view_tool(
                self.conn, cfg.panel_title, TOOL_ID, False, f"http://127.0.0.1:{self.engine.port}/panel?t={self.engine.token}")
        if "features.title_spinner" in changed_keys and not cfg.title_spinner:
            for sid, look in list(self.looks.items()):  # put plain titles back on tabs mid-spin
                s = self.app.get_session_by_id(sid)
                if s and look.state == "working" and look.title:
                    await self.set_name(s, look.title)

    def session(self, pane):
        return self.app.get_session_by_id(pane.ref)

    def windows(self):
        return [w for w in self.app.terminal_windows if not self.cfg.iterm_window or w.window_id == self.cfg.iterm_window]

    def all_sessions(self):
        return [s for w in self.windows() for t in w.tabs for s in t.sessions]

    # --- reading ---

    async def panes(self):
        await self.app.async_refresh_focus()  # the App object doesn't track focus changes by itself
        current = None
        w = self.app.current_terminal_window
        if w and w.current_tab:
            current = w.current_tab.current_session.session_id
        out = []
        # Tabs outside the watched window still exist; their hibernated records must not be dropped.
        self.all_keys = {f"{self.prefix}:{s.session_id}" for w in self.app.terminal_windows for t in w.tabs for s in t.sessions}
        for wi, window in enumerate(self.windows()):
            for ti, tab in enumerate(window.tabs):
                for si, s in enumerate(tab.sessions):
                    sid = s.session_id
                    if sid not in self.ttys:
                        self.ttys[sid] = await s.async_get_variable("tty")
                    name = await s.async_get_variable("autoName") or ""
                    out.append(Pane(key=f"{self.prefix}:{sid}", tty=self.ttys[sid], name=name,
                                    focused=sid == current, shell=False, order=(wi, ti, si), term=self, ref=sid))
        return out

    async def screen(self, pane, drop_faint=False):
        c = await self.session(pane).async_get_screen_contents()
        lines = [c.line(i) for i in range(c.number_of_lines)]
        # iTerm2 gives never-written cells as NUL; agents' checks expect spaces.
        return [(unfaint(l) if drop_faint else l.string).replace("\0", " ") for l in lines], c.cursor_coord.y

    async def shell_ready(self, pane):
        s = self.session(pane)
        return bool(s) and is_shell(await s.async_get_variable("jobName"))

    async def call(self, s, expr):
        try:
            r = await s.async_invoke_function(expr, timeout=5)
            return r if isinstance(r, dict) else {}
        except Exception:  # pylint: disable=broad-except
            return {}

    async def agent_status(self, pane):
        """iTerm 3.6+ tracks Claude Code's state itself (working / waiting / idle, with a detail line)."""
        s = self.session(pane)
        st = await self.call(s, f'iterm2.get_session_status(session_id: "{s.session_id}")')
        if (st.get("status") or "").startswith("hibernated "):  # ours, left over from before a wake
            await self.set_status(s, "", "")
            self.applied.get(s.session_id, {}).pop("status", None)
            return {}
        return st

    # --- writing ---

    async def send(self, pane, text):
        await self.session(pane).async_send_text(text)

    async def push(self, s, key, value, setter):
        """Run setter only when value changed since the last push for this tab."""
        seen = self.applied.setdefault(s.session_id, {})
        if seen.get(key, object()) == value:
            return
        await setter()
        seen[key] = value

    async def set_status(self, s, text, color):
        q, c = json.dumps(text, ensure_ascii=False), json.dumps(color)
        await self.call(s, f'iterm2.set_session_status(session_id: "{s.session_id}", status: {q}, '
                           f'dot_color: {c}, text_color: {c})')

    async def set_title_mode(self, s, hibernated):
        """Hibernated tabs drop the job part so they read 'zz <title>' rather than 'zz <title> (-zsh)'.

        (A per-tab title function would be cleaner, but iTerm 3.7's per-session setter only accepts a
        string for "Title Function" while titles read it as a pair, so it never fires.)
        """
        async def setter():
            saved = self.applied.setdefault(s.session_id, {})
            if hibernated:
                if "title_before" not in saved:  # iTerm adds bits of its own at runtime; keep them
                    saved["title_before"] = (await s.async_get_profile()).all_properties.get("Title Components")
                value = iterm2.profile.TitleComponents.SESSION_NAME.value
            else:
                value = saved.pop("title_before", None) or await self.base_value(s, "Title Components", 3)
            await s.async_set_profile_properties(iterm2.LocalWriteOnlyProfile({"Title Components": value}))
        await self.push(s, "title_mode", hibernated, setter)

    async def base_value(self, s, key, default):
        """The value `key` has in the profile the tab came from (what to put back on wake)."""
        try:
            p = await s.async_get_profile()
            guid = p.original_guid or p.guid
            if (guid, key) not in self.base_props:
                found = await iterm2.PartialProfile.async_query(self.conn, guids=[guid], properties=[key])
                self.base_props[(guid, key)] = found[0].all_properties.get(key, default) if found else default
            return self.base_props[(guid, key)]
        except Exception:  # pylint: disable=broad-except
            return default

    async def set_icon(self, s, sleeping):
        async def setter():
            p = iterm2.LocalWriteOnlyProfile({"Icon": await self.base_value(s, "Icon", 1)})
            if sleeping:
                p.set_icon_mode(iterm2.profile.IconMode.CUSTOM)
                p.set_custom_icon_path(self.icon)
            await s.async_set_profile_properties(p)
        await self.push(s, "icon", sleeping, setter)

    async def set_name(self, s, name):
        """Agents overwrite the tab name on each status change; remember ours to tell them apart."""
        self.my_names[s.session_id] = name
        await s.async_set_name(name)

    async def set_var(self, s, name, value):
        await self.push(s, name, value, lambda: s.async_set_variable(name, value))

    async def apply(self, pane, look):
        s = self.session(pane)
        if not s:
            return
        sid = s.session_id
        seen = self.applied.get(sid, {})
        if look is None:
            self.looks.pop(sid, None)
            if seen.get("title_mode"):
                await self.set_title_mode(s, False)
            if seen.get("icon"):
                await self.set_icon(s, False)
            if seen.get("status"):
                await self.set_status(s, "", "")
                seen.pop("status")
            return
        self.looks[sid] = look
        if look.state in ("hibernated", "waking"):
            # Put the title setting back before the agent starts: iTerm2 adds its own title bits at
            # agent start and restores what it saw then on exit, so restoring later would wipe them.
            await self.set_title_mode(s, look.state == "hibernated")
            name = f"{'…' if look.state == 'waking' else 'zz'} {look.title}"
            if pane.name != name:
                await self.set_name(s, name)
            if self.cfg.tab_icon:
                await self.set_icon(s, True)
            elif seen.get("icon"):
                await self.set_icon(s, False)
            await self.push(s, "status", look.status_text, lambda: self.set_status(s, look.status_text, PURPLE))
            await self.set_var(s, BAR_VAR, look.bar)
            return
        if seen.get("title_mode"):
            await self.set_title_mode(s, False)
        if seen.get("icon"):
            await self.set_icon(s, False)
        if seen.get("status"):
            await self.set_status(s, "", "")
            seen.pop("status")
        await self.set_var(s, BAR_VAR, look.bar)
        if look.state == "idle" and look.title:
            stale = pane.name == self.my_names.get(sid) or strip_glyph(pane.name) in PLACEHOLDER_TITLES
            want = f"{look.idle_glyph} {look.title}".strip()
            if stale and pane.name != want:
                await self.set_name(s, want)  # a spinner frame outlived the turn, or iTerm reset the name

    async def pulse(self, pane, on):
        s = self.session(pane)
        if not s:
            return
        p = iterm2.LocalWriteOnlyProfile()
        if on:
            p.set_tab_color(iterm2.Color(*PULSE_RGB))
        p.set_use_tab_color(on)
        await s.async_set_profile_properties(p)
        self.applied.setdefault(s.session_id, {})["pulse"] = on

    async def focus(self, pane):
        await self.session(pane).async_activate(select_tab=True, order_window_front=True)

    async def revert_all(self):
        for s in self.all_sessions():
            pane = Pane(key=f"{self.prefix}:{s.session_id}", tty=None, name="", focused=False,
                        shell=False, order=(), term=self, ref=s.session_id)
            await self.apply(pane, None)
            if self.applied.get(s.session_id, {}).get("pulse"):
                await self.pulse(pane, False)

    async def adopt_existing_decorations(self):
        """After a daemon restart, remember which tabs still carry our title mode, icon or tab color."""
        for s in self.all_sessions():
            try:
                p = await s.async_get_profile()
            except Exception:  # pylint: disable=broad-except
                continue
            seen = self.applied.setdefault(s.session_id, {})
            # Raw value: the library's title_components getter rejects bits newer than it knows.
            if p.all_properties.get("Title Components") == iterm2.profile.TitleComponents.SESSION_NAME.value:
                seen["title_mode"] = True
            if getattr(p, "custom_icon_path", None) in (ICON_PATH, self.icon) and \
                    p.icon_mode == iterm2.profile.IconMode.CUSTOM:
                seen["icon"] = True
            tab = p.all_properties.get("Tab Color") or {}
            if p.all_properties.get("Use Tab Color") and \
                    round(tab.get("Red Component", -1) * 255) == PULSE_RGB[0] and \
                    round(tab.get("Green Component", -1) * 255) == PULSE_RGB[1]:
                await self.pulse(Pane(key="", tty=None, name="", focused=False, shell=False, order=(),
                                      term=self, ref=s.session_id), False)

    # --- background ---

    async def run(self, engine):
        await asyncio.gather(self.run_spinner(), self.run_keystrokes())

    async def run_spinner(self):
        """Animate the title of working tabs. Claude titles itself '✳ <title>' when idle and plain
        '<title>' while working, so only working tabs need a glyph from us."""
        while self.up:
            self.frame = (self.frame + 1) % len(SPINNER)
            for sid, look in list(self.looks.items()):
                if look.state != "working" or not self.cfg.title_spinner:
                    continue
                s = self.app.get_session_by_id(sid)
                if not s:
                    continue
                try:
                    current = await s.async_get_variable("autoName") or ""
                    if current != self.my_names.get(sid) and look.idle_glyph and current.startswith(look.idle_glyph):
                        look.state = "idle"  # the agent just went idle; leave its title alone
                        continue
                    if look.title:
                        await self.set_name(s, f"{SPINNER[self.frame]} {look.title}")
                except Exception:  # pylint: disable=broad-except
                    pass
            await asyncio.sleep(SPIN_SECONDS)

    async def run_keystrokes(self):
        async with iterm2.KeystrokeMonitor(self.conn) as mon:
            while self.up:
                n = await mon.async_get()
                sid = getattr(n, "session", None)
                if sid:
                    self.keys[f"{self.prefix}:{sid}"] = time.time()


async def install_status_bar_button(connection, remove=False):
    """Append (or remove) a Custom Action button (title \\(user.tabnap_bar), click -> tabnap_toggle) on each
    profile that shows a status bar."""
    for partial in await iterm2.PartialProfile.async_query(connection):
        profile = await partial.async_get_full_profile()
        props = profile.all_properties
        layout = props.get("Status Bar Layout")
        if not layout or not props.get("Show Status Bar"):
            continue
        components = layout.setdefault("components", [])

        def ours(c):
            return c.get("configuration", {}).get("knobs", {}).get("action", {}).get("parameter") == TOGGLE_INVOCATION

        if remove:
            kept = [c for c in components if not ours(c)]
            if len(kept) != len(components):
                layout["components"] = kept
                await profile._async_simple_set("Status Bar Layout", layout)  # pylint: disable=protected-access
                print(f"  {profile.name}: removed the tabnap button")
            continue
        if any(ours(c) for c in components):
            print(f"  {profile.name}: already has the button")
            continue
        components.append({
            "class": "iTermStatusBarActionComponent",
            "configuration": {
                "knobs": {
                    "action": {"action": 60,  # KEY_ACTION_INVOKE_SCRIPT_FUNCTION
                               "title": "\\(" + BAR_VAR + ")", "parameter": TOGGLE_INVOCATION,
                               "version": 2, "escaping": 0, "applyMode": 0},
                    "base: priority": 5,
                    "base: compression resistance": 1,
                },
                "layout advanced configuration dictionary value": layout.get("advanced configuration", {}),
            },
        })
        await profile._async_simple_set("Status Bar Layout", layout)  # pylint: disable=protected-access
        print(f"  {profile.name}: added the tabnap button to the status bar")


SLEEPING_ICON = [  # a crescent moon with z's, for hibernated tabs
    "................",
    "..........zzz...",
    "............z...",
    "....mmm....z....",
    "..mmmm....zzz...",
    ".mmmm...........",
    ".mmm............",
    "mmmm............",
    "mmmm............",
    "mmmm..........m.",
    ".mmmm........mm.",
    ".mmmmm.....mmm..",
    "..mmmmmmmmmmmm..",
    "...mmmmmmmmmm...",
    ".....mmmmmm.....",
    "................",
]


def write_icon(path, scale=4):
    colors = {".": (0, 0, 0, 0), "m": (168, 157, 224, 255), "z": (197, 189, 242, 255)}
    raw = b""
    for row in SLEEPING_ICON:
        raw += (b"\x00" + b"".join(bytes(colors[ch]) * scale for ch in row)) * scale
    w, h = len(SLEEPING_ICON[0]) * scale, len(SLEEPING_ICON) * scale

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png)
