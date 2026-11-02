"""`tabnap install`: ask which features you want, wire up your terminals, start the daemon at login."""
import os
import shutil
import subprocess
import sys

from tabnap import config, service
from tabnap.config import CONFIG_FILE, HOME, LOG_FILE

TMUX_LINE_TAG = "# added by tabnap"


class Prompter:
    def __init__(self, assume_yes):
        self.assume_yes = assume_yes
        self.tty = None
        if not assume_yes:
            try:
                self.tty = sys.stdin if sys.stdin.isatty() else open("/dev/tty")  # works under `curl | sh` too
            except OSError:
                self.assume_yes = True

    def ask(self, question, default):
        if self.assume_yes:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        while True:
            print(f"{question} {hint} ", end="", flush=True)
            a = self.tty.readline().strip().lower()
            if not a:
                return default
            if a in ("y", "yes", "n", "no"):
                return a.startswith("y")

    def number(self, question, default):
        if self.assume_yes:
            return default
        print(f"{question} [{default:g}] ", end="", flush=True)
        a = self.tty.readline().strip()
        try:
            return float(a) if a else default
        except ValueError:
            return default


def tmux_conf_path():
    xdg = os.path.join(HOME, ".config/tmux/tmux.conf")
    return xdg if os.path.exists(xdg) and not os.path.exists(os.path.join(HOME, ".tmux.conf")) else \
        os.path.join(HOME, ".tmux.conf")


def setup_tmux(exe):
    from tabnap.terminals.tmux import SNIPPET
    snippet = os.path.join(os.path.dirname(CONFIG_FILE), "tabnap.tmux")
    with open(snippet, "w") as fh:
        fh.write(SNIPPET.replace("{tabnap}", exe))
    conf = tmux_conf_path()
    line = f"source-file {snippet}  {TMUX_LINE_TAG}"
    existing = open(conf).read() if os.path.exists(conf) else ""
    if TMUX_LINE_TAG not in existing:
        with open(conf, "a") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + line + "\n")
    subprocess.run(["tmux", "source-file", conf], capture_output=True)  # apply now if a server is running
    return conf


def remove_tmux():
    conf = tmux_conf_path()
    if not os.path.exists(conf):
        return
    with open(conf) as fh:
        lines = fh.readlines()
    kept = [l for l in lines if TMUX_LINE_TAG not in l]
    if len(kept) != len(lines):
        with open(conf, "w") as fh:
            fh.writelines(kept)
        print(f"Removed the tabnap line from {conf}.")


def iterm_installed():
    return sys.platform == "darwin" and (os.path.isdir("/Applications/iTerm.app") or
                                         os.path.isdir(os.path.join(HOME, "Applications/iTerm.app")))


def iterm_api_enabled():
    out = subprocess.run(["defaults", "read", "com.googlecode.iterm2", "EnableAPIServer"],
                         capture_output=True, text=True).stdout.strip()
    return out == "1"


def run(assume_yes=False):
    p = Prompter(assume_yes)
    c = config.load()
    exe = shutil.which("tabnap") or f"{sys.executable} -m tabnap"
    print("tabnap puts idle Claude Code and Codex sessions to sleep (the agent quits, the tab stays, the resume")
    print("command waits at the prompt) and wakes them in place.\n")

    auto = p.ask("Auto-hibernate sessions that sit idle?", False)
    if auto:
        c.idle_minutes = p.number("  After how many idle minutes?", c.idle_minutes)
        c.max_awake = int(p.number("  Also keep at most this many sessions awake? The longest-idle ones sleep first (0 = no cap)",
                                   c.max_awake))
    c.context = p.ask("Show your last prompt and the agent's last reply in the session list?\n"
                      "  (read from the agent's local transcript; nothing leaves your machine)", c.context)
    c.needs_pulse = p.ask("Blink a tab when its agent finishes or needs you while you're in another tab?",
                          c.needs_pulse)

    wants_iterm = False
    if iterm_installed():
        wants_iterm = p.ask("Set up iTerm2 (tab decorations, a sidebar panel, a status bar button)?", True)
        if wants_iterm:
            c.title_spinner = p.ask("  Animate the titles of working tabs?", c.title_spinner)
            c.tab_icon = p.ask("  Show a sleeping icon on hibernated tabs?", c.tab_icon)
            if not iterm_api_enabled() and p.ask("  tabnap needs iTerm2's Python API. Turn it on now?", True):
                subprocess.run(["defaults", "write", "com.googlecode.iterm2", "EnableAPIServer", "-bool", "true"])
                print("  Done. Restart iTerm2 if it is running so the setting takes effect.")
    wants_tmux = bool(shutil.which("tmux")) and p.ask(
        "Set up tmux (state labels in the status line, prefix+N opens the session list)?", True)
    c.terminals = [t for t, on in (("iterm", wants_iterm), ("tmux", wants_tmux)) if on]
    if not c.terminals:
        print("  No terminal picked, so the daemon will have nothing to watch. tabnap supports iTerm2 and tmux"
              " (tmux runs inside any terminal).")

    config.write(c)
    print(f"\nSaved your choices to {CONFIG_FILE}")
    from tabnap.engine import load_state, save_state
    st = load_state()
    st["auto"] = auto  # the daemon reads the switch from its state file (the panel toggles it there too)
    save_state(st)

    if wants_tmux:
        print(f"tmux: added a source-file line to {setup_tmux(exe)}")
    if p.ask("Start tabnap now and at every login?", True):
        path = service.install()
        print(f"Daemon running ({path}); log: {LOG_FILE}")
    else:
        print("Not starting it. Run `tabnap daemon` yourself when you want it.")
    if wants_iterm:
        try:
            import iterm2
            from tabnap.terminals.iterm import install_status_bar_button, iterm_running
            if iterm_running() and iterm_api_enabled():
                print("iTerm2 status bar:")
                iterm2.run_until_complete(install_status_bar_button)
            else:
                print("iTerm2: open iTerm2, then run `tabnap setup iterm` to add the status bar button.")
        except ImportError:
            print("iTerm2 support needs the extra: uv tool install 'tabnap[iterm]' (or pipx install 'tabnap[iterm]').")
        print(f"iTerm2 sidebar: View > Toolbelt > {c.panel_title}.")
    print("\nTry it: tabnap status · tabnap top · tabnap panel")
