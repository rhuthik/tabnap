"""tabnap: hibernate idle Claude Code / Codex sessions in your terminal tabs, and wake them in place.

  tabnap install            pick features, set up iTerm2 / tmux, run the daemon at login (--yes: defaults)
  tabnap status             agent sessions and their state
  tabnap top                interactive list (works in any terminal; bind it to a tmux popup)
  tabnap hibernate <tab>    <tab>: part of the title, an agent name, or the start of the tab id
  tabnap hibernate --idle   put every idle session to sleep (pinned, focused and busy ones stay awake)
  tabnap wake <tab>
  tabnap refresh <tab>      hibernate + wake: same conversation, fresh process (drops leaked memory)
  tabnap pin <tab>          keep awake: never auto-hibernate (unpin to undo)
  tabnap auto on|off        auto-hibernate idle sessions after idle_minutes (default 40)
  tabnap panel              open the session panel in your browser
  tabnap setup tmux         status-line format + popup key for tmux
  tabnap setup iterm        add the Wake/Hibernate button to iTerm2 status bars
  tabnap config             show every setting; `tabnap config set <key> <value>` changes one (applied live)
  tabnap doctor             check what tabnap can see
  tabnap snapshot           the newest crash snapshot as paste-able resume commands
  tabnap start | stop       start the daemon now and at login / stop it (no questions asked)
  tabnap restart            restart the daemon (after editing the config)
  tabnap daemon             run in the foreground (what install runs)
  tabnap uninstall          stop the daemon and take decorations off
"""
import asyncio
import json
import os
import shlex
import shutil
import signal
import sys
import webbrowser

from tabnap import __version__, config, procs, service
from tabnap.config import CONFIG_FILE, DAEMON_FILE, LOG_FILE, STATE_DIR
from tabnap.engine import Engine, fmt_minutes, log
from tabnap.server import api


def build_terminals(cfg):
    terms = []
    for name in cfg.terminals:
        if name == "iterm" and sys.platform == "darwin":
            try:
                from tabnap.terminals.iterm import ITerm
            except ImportError:
                log("iTerm2 support needs the iterm2 package: install tabnap[iterm]")
                continue
            terms.append(ITerm(cfg))
        elif name == "tmux":
            from tabnap.terminals.tmux import Tmux
            terms.append(Tmux(cfg))
            terms += [Tmux(cfg, s) for s in cfg.tmux_sockets]
    return terms


async def daemon_main():
    from tabnap import server
    cfg = config.load()
    os.makedirs(STATE_DIR, exist_ok=True)
    engine = Engine(cfg, build_terminals(cfg))
    token = server.load_token()
    engine.port = server.serve(engine, asyncio.get_running_loop(), cfg.port, token)
    engine.token = token
    log(f"tabnap {__version__} up on 127.0.0.1:{engine.port}; {len(engine.state['tabs'])} hibernated tab(s) on "
        f"record; auto={'on' if engine.state['auto'] else 'off'}; terminals: {', '.join(t.kind for t in engine.terminals)}")
    await asyncio.gather(engine.run_mem(), engine.run_ticks(), engine.run_pulse(), engine.run_snapshots())


def single_instance():
    """Hold a lock for the daemon's lifetime: two daemons would fight over the same tabs."""
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    fh = open(os.path.join(STATE_DIR, "daemon.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f"another tabnap daemon is already running (lock: {fh.name})")
    return fh


def run_daemon():
    lock = single_instance()  # noqa: F841  (kept open until exit)

    def on_term(_signum, _frame):
        try:
            with open(DAEMON_FILE) as fh:
                if json.load(fh).get("pid") == os.getpid():
                    os.remove(DAEMON_FILE)
        except (OSError, ValueError):
            pass
        os._exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    asyncio.run(daemon_main())


def resolve(arg):
    rows = api("/api/state", method="GET")["sessions"]
    a = arg.lower()
    hits = [r for r in rows if r["id"].split(":", 1)[-1].lower().startswith(a)] or \
           [r for r in rows if a in r["title"].lower()] or [r for r in rows if r["agent"] == a]
    if len(hits) != 1:
        names = "\n  ".join(r["title"] for r in hits) if hits else "(none)"
        raise SystemExit(f"'{arg}' matches {len(hits)} sessions:\n  {names}")
    return hits[0]


def cmd_status():
    st = api("/api/state", method="GET")
    for r in st["sessions"]:
        if r["state"] == "hibernated":
            what = f"hibernated {fmt_minutes(r['asleep_min'])}, freed {r['freed_mb']} MB"
        elif r["state"] == "idle":
            what = f"idle {fmt_minutes(r['idle_min'] or 0)}, {r['mem_mb']} MB"
            what += f", sleeps in {fmt_minutes(r['sleeps_in'])}" if r.get("sleeps_in") is not None else ""
            what += f" ({r['blocker']})" if r.get("blocker") else ""
        else:
            what = f"{r['state']}, {r['mem_mb']} MB"
        pin = " [kept awake]" if r.get("pinned") else ""
        print(f"  {r['agent']:<6} {r['id'].split(':', 1)[-1][:10]:<10}  {r['title'][:56]:<56}  {what}{pin}")
    print(f"{st['awake']} awake ({st['in_use_mb'] / 1024:.1f} GB), {st['asleep']} hibernated "
          f"({st['freed_mb'] / 1024:.1f} GB freed); auto-hibernate {'on' if st['auto'] else 'off'} "
          f"after {st['idle_minutes']:g} min; terminals: {', '.join(st.get('terminals') or []) or 'none'}")


def cmd_panel():
    try:
        with open(DAEMON_FILE) as fh:
            info = json.load(fh)
    except (OSError, ValueError):
        raise SystemExit("tabnap isn't running. Start it with `tabnap start` (or `tabnap install` to set it up).")
    url = f"http://127.0.0.1:{info['port']}/panel?t={info['token']}"
    webbrowser.open(url)
    print("Opened the panel in your browser.")


def cmd_setup(what):
    if what == "tmux":
        from tabnap.terminals.tmux import SNIPPET
        exe = shutil.which("tabnap") or f"{sys.executable} -m tabnap"
        path = os.path.join(os.path.dirname(CONFIG_FILE), "tabnap.tmux")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(SNIPPET.replace("{tabnap}", exe))
        print(f"Wrote {path}. Add this line to ~/.tmux.conf, then reload tmux:\n\n  source-file {path}\n")
        return
    if what == "iterm":
        from tabnap.terminals.iterm import install_status_bar_button
        import iterm2
        iterm2.run_until_complete(install_status_bar_button)
        print(f"Sidebar panel: View > Toolbelt > {config.load().panel_title}.")
        return
    raise SystemExit("usage: tabnap setup tmux|iterm")


def cmd_config(args):
    """tabnap config [set <key> <value> | path | edit | init]"""
    sub = args[0] if args else ""
    if sub == "path":
        return print(CONFIG_FILE)
    if sub == "init":
        if os.path.exists(CONFIG_FILE):
            return print(f"{CONFIG_FILE} already exists.")
        config.write(config.Config())
        return print(f"Wrote {CONFIG_FILE}")
    if sub == "edit":
        if not os.path.exists(CONFIG_FILE):
            config.write(config.load())
        os.system(f"{os.environ.get('EDITOR', 'vi')} {shlex.quote(CONFIG_FILE)}")
        return report_reload(try_api("/api/reload"))
    if sub == "set":
        if len(args) < 3:
            raise SystemExit("usage: tabnap config set <key> <value>   (see `tabnap config` for keys)")
        key, value = args[1], " ".join(args[2:])
        result = try_api("/api/config", key=key, value=value)
        if result is None:  # daemon not running: just save it
            try:
                config.set_value(key, value)
            except (KeyError, ValueError) as e:
                raise SystemExit(str(e).strip("'\""))
            return print(f"Saved {key}. The daemon isn't running; it'll use this when it starts.")
        return report_reload(result, key)
    if sub:
        raise SystemExit("usage: tabnap config [set <key> <value> | path | edit | init]")
    cfg = config.load()
    print(f"# {CONFIG_FILE}")
    for key, value, help_, restart in config.describe(cfg):
        shown = config.toml_value(value) if not isinstance(value, dict) else (json.dumps(value) if value else "{}")
        print(f"{key:<24} = {shown:<28}  # {help_}{' (restart)' if restart else ''}")
    st = try_api("/api/state", method="GET")
    print(f"{'auto':<24} = {'on' if st and st.get('auto') else 'off':<28}  # the auto-hibernate switch: tabnap auto on|off")


def try_api(path, method="POST", **params):
    """api() that returns None when the daemon isn't running, and exits on a real error."""
    if not os.path.exists(DAEMON_FILE):
        return None
    try:
        return api(path, method=method, **params)
    except SystemExit as e:
        if "not reachable" in str(e) or "isn't running" in str(e):
            return None
        raise


def report_reload(result, key=None):
    if result is None:
        return print("Saved. The daemon isn't running; it'll use this when it starts.")
    restart = result.get("restart") or []
    what = f"Saved {key}" if key else "Reloaded"
    if restart:
        print(f"{what}. These need a restart to take effect: {', '.join(restart)} (tabnap restart)")
    else:
        print(f"{what}; applied now.")


def cmd_snapshot():
    """Print the newest crash snapshot as commands you can paste, one tab per line."""
    import glob
    import re
    cfg = config.load()
    if not cfg.snapshot_dir:
        raise SystemExit("Snapshots are off. Turn them on: tabnap config set snapshot.dir ~/.local/state/tabnap/snapshots")
    pattern = re.sub(r"%[a-zA-Z]", "*", cfg.snapshot_name)
    files = sorted(glob.glob(os.path.join(cfg.snapshot_dir, pattern)))
    if not files:
        raise SystemExit(f"No snapshots in {cfg.snapshot_dir} yet (one is written every {cfg.snapshot_minutes:g} min).")
    with open(files[-1]) as fh:
        rows = json.load(fh)
    from tabnap.engine import cd_arg
    print(f"# {files[-1]}: {len(rows)} sessions. Open a tab per line and paste it.")
    for r in rows:
        if r.get("command"):
            print(f"cd {cd_arg(r['cwd'])} && {r['command']}    # {r.get('title') or ''}{' (was hibernated)' if r.get('hibernated') else ''}")


def cmd_doctor():
    from tabnap.agents import top_level
    cfg = config.load()
    print(f"tabnap {__version__}, python {sys.version.split()[0]}, {sys.platform}")
    print(f"config: {CONFIG_FILE} ({'found' if os.path.exists(CONFIG_FILE) else 'defaults'})")
    print(f"state:  {STATE_DIR}")
    print(f"claude config dirs: {', '.join(cfg.claude_dirs()) or 'none'}")
    print(f"codex home: {cfg.codex_home} ({'found' if os.path.isdir(cfg.codex_home) else 'missing'})")
    print(f"tmux: {shutil.which('tmux') or 'not installed'}")
    if sys.platform == "darwin":
        try:
            import iterm2  # noqa: F401
            print("iterm2 python package: installed")
        except ImportError:
            print("iterm2 python package: missing (pip install 'tabnap[iterm]' for iTerm2 support)")
    found = top_level(procs.process_table())
    print(f"agent sessions on terminals: {len(found)}")
    for tty, (pid, agent) in sorted(found.items()):
        live = agent.inspect(pid, cfg)
        print(f"  {tty:<14} {agent.name:<6} pid {pid:<7} {live.status:<8} session {live.session_id or '-'}"
              f"{'' if live.confident else ' (guess)'}")
    try:
        st = api("/api/state", method="GET")
        print(f"daemon: running; connected to: {', '.join(st.get('terminals') or []) or 'no terminal (see above)'}")
    except SystemExit as e:
        print(f"daemon: {e}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "daemon":
        return run_daemon()
    if cmd == "status":
        return cmd_status()
    if cmd == "top":
        from tabnap import top
        return top.main()
    if cmd == "hibernate" and args == ["--idle"]:
        result = api("/api/hibernate_idle")
        for t in result["slept"]:
            print(f"hibernated: {t}")
        for t, why in result["skipped"].items():
            print(f"left awake: {t} ({why})")
        return print(f"{len(result['slept'])} put to sleep, {len(result['skipped'])} left awake.")
    if cmd in ("hibernate", "wake", "refresh", "pin", "unpin") and args:
        row = resolve(" ".join(args))
        api(f"/api/{cmd}", id=row["id"])
        return print(f"{cmd}: {row['title']}")
    if cmd == "auto" and args and args[0] in ("on", "off"):
        api("/api/auto", on="1" if args[0] == "on" else "0")
        return print(f"auto-hibernate {args[0]}")
    if cmd == "panel":
        return cmd_panel()
    if cmd == "setup" and args:
        return cmd_setup(args[0])
    if cmd == "config":
        return cmd_config(args)
    if cmd == "doctor":
        return cmd_doctor()
    if cmd == "snapshot":
        return cmd_snapshot()
    if cmd == "install":
        from tabnap import wizard
        return wizard.run(assume_yes="--yes" in args or "-y" in args)
    if cmd == "start":
        path = service.install()
        return print(f"Daemon running and starting at login ({path}); log: {LOG_FILE}")
    if cmd == "stop":
        path = service.uninstall()
        return print(f"Daemon stopped and won't start at login ({path} removed). Decorations stay; tabnap start brings it back.")
    if cmd == "restart":
        return print("Restarted." if service.restart() else "No tabnap service to restart (run tabnap install).")
    if cmd == "uninstall":
        try:
            api("/api/revert")
        except SystemExit as e:
            print(f"Could not take decorations off: {e}")
        if sys.platform == "darwin":
            try:
                import iterm2
                from tabnap.terminals.iterm import install_status_bar_button, iterm_running
                if iterm_running():
                    iterm2.run_until_complete(lambda c: install_status_bar_button(c, remove=True))
            except ImportError:
                pass
        from tabnap import wizard
        wizard.remove_tmux()
        path = service.uninstall()
        print(f"Daemon stopped ({path} removed). Hibernated tabs keep their pre-typed resume command.")
        return
    if cmd in ("-V", "--version", "version"):
        return print(__version__)
    print(__doc__)
    sys.exit(0 if cmd in ("", "-h", "--help", "help") else 1)
