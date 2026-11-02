"""The terminal- and agent-agnostic part: find agent sessions, decide, hibernate, wake."""
import asyncio
import datetime
import glob
import json
import os
import re
import shlex
import signal
import threading
import time
import traceback

from tabnap import procs
from tabnap.agents import BY_NAME, top_level
from tabnap.config import STATE_DIR, STATE_FILE
from tabnap.terminals.base import PLACEHOLDER_TITLES, Look, is_shell

TICK_SECONDS = 3
MEM_SECONDS = 60
PULSE_SECONDS = 0.8
GLYPH_RE = re.compile(r"^[^\w\s(\[\"'`]{1,2}\s+")


def strip_glyph(name):
    """'✳ Fix the parser' -> 'Fix the parser' (agents prefix their tab title with a status glyph)."""
    name = (name or "").strip()
    name = re.sub(r" \((claude|codex|node)\)$", "", name)
    return GLYPH_RE.sub("", name).strip()


def fmt_minutes(m):
    m = int(max(0, m))
    if m < 60:
        return f"{m}m"
    if m < 48 * 60:
        return f"{m // 60}h{m % 60:02d}m" if m % 60 else f"{m // 60}h"
    return f"{m // 1440}d"


def auto_decision(f, now, idle_minutes):
    """(blocker, minutes_left). blocker is a reason auto-hibernate won't run; minutes_left <= 0 means due."""
    if f["pinned"]:
        return "kept awake", None
    if f["status"] in ("working", "waiting"):
        return f["status"], None
    if not f["transcript"]:
        return "nothing to resume yet", None
    if not f["confident"]:
        return "session match is a guess", None
    if f["background"]:
        return "background job running", None
    if f["loop_tool"]:
        return f"uses {f['loop_tool']}", None
    if f["focused"]:
        return "tab in focus", None
    return None, idle_minutes - (now - f["last_active"]) / 60


def cd_arg(path):
    """Shell-quoted directory, written as ~/... when it's under your home (shorter, same meaning)."""
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if path.startswith(home + "/"):
        return "~/" + shlex.quote(path[len(home) + 1:])
    return shlex.quote(path)


def log(msg):
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_state(path=None):
    path = path or STATE_FILE
    try:
        with open(path) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("tabs", {})      # pane key -> hibernated tab record
    st.setdefault("pinned", [])    # agent session ids that are never auto-hibernated
    st.setdefault("auto", False)
    return st


def save_state(st, path=None):
    path = path or STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, path)


class ActionError(Exception):
    pass


class Engine:
    def __init__(self, cfg, terminals):
        self.cfg = cfg
        self.terminals = terminals
        self.state = load_state()
        self.rows = []            # snapshot for the panel/CLI, rebuilt every tick
        self.live = {}            # pane key -> (pane, Live, facts)
        self.panes = {}           # pane key -> Pane (every pane seen last tick)
        self.busy_seen = {}       # pane key -> last time it was working/waiting
        self.mem = {}             # pid -> MB
        self.acting = set()       # pane keys with a hibernate/wake in flight
        self.missing = {}         # pane key -> ticks it has been absent
        self.cache = {}           # (kind, transcript) -> (mtime, value)
        self.prev_status = {}     # pane key -> status at the previous tick
        self.prev_status_at = {}  # pane key -> the agent's status timestamp at the previous tick
        self.quiet_until = {}     # pane key -> no "needs you" before this (a resumed agent settles first)
        self.needs = {}           # pane key -> "done" | "waiting": finished or blocked while you were elsewhere
        self.lit = set()          # pane keys whose pulse is on
        self.snap_lock = threading.Lock()
        self.retry_at = {}        # terminal -> when to try connecting again
        self.mem_wanted = None    # asyncio.Event: a new agent pid has no memory sample yet
        self.kick = None          # asyncio.Event: run the next tick now (after an action)
        self.waiters = []         # futures resolved when the next full tick finishes

    # --- helpers ---

    def cached(self, kind, path, fn):
        try:
            mtime = os.path.getmtime(path)
        except (OSError, TypeError):
            return None
        hit = self.cache.get((kind, path))
        if not hit or hit[0] != mtime:
            hit = (mtime, fn())
            self.cache[(kind, path)] = hit
        return hit[1]

    def title_for(self, pane, live):
        t = self.cached("title", live.transcript, lambda: live.agent.title(live)) if live.transcript else None
        if live.agent.name == "codex":
            t = live.agent.title(live)  # comes from the thread row, already fresh
        if t:
            return t
        name = strip_glyph(pane.name)
        return name if name not in PLACEHOLDER_TITLES else (live.cwd or "").rstrip("/").split("/")[-1] or live.agent.label

    def context(self, agent, transcript):
        if not self.cfg.context:
            return {}  # off: don't even read the transcript
        return self.cached("ctx", transcript, lambda: agent.context(transcript)) or {}

    async def connect(self):
        now = time.time()
        for term in self.terminals:
            if term.up or self.retry_at.get(term, 0) > now:
                continue
            try:
                term.up = await term.start(self)
            except Exception as e:  # pylint: disable=broad-except
                log(f"{term.kind}: can't connect ({type(e).__name__}: {e})")
                term.up = False
            if term.up:
                log(f"{term.kind}: connected")
                asyncio.ensure_future(self.guard(term, term.run(self)))
            else:
                self.retry_at[term] = now + 30

    async def guard(self, term, coro):
        try:
            await coro
        except Exception as e:  # pylint: disable=broad-except
            log(f"{term.kind}: background loop stopped ({type(e).__name__}: {e})")

    # --- main loop ---

    async def tick(self):
        now = time.time()
        await self.connect()
        table = procs.process_table()
        kids = procs.children_map(table)
        agents = top_level(table)
        rows, live, panes = [], {}, {}
        claimed = set()
        seen_terms = set()
        for term in self.terminals:
            if not term.up:
                continue
            try:
                term_panes = await term.panes()
            except Exception as e:  # pylint: disable=broad-except
                log(f"{term.kind}: lost ({type(e).__name__}: {e})")
                term.up = False
                continue
            seen_terms.add(term)
            for pane in term_panes:
                panes[pane.key] = pane
                found = agents.get(pane.tty) if pane.tty else None
                hib = self.state["tabs"].get(pane.key)
                if found:
                    pid, agent = found
                    if hib:
                        await self.on_woke(pane, hib)
                    info = agent.inspect(pid, self.cfg, frozenset(claimed))
                    if info.session_id:
                        claimed.add(info.session_id)
                    if info.pid not in self.mem and self.mem_wanted:
                        self.mem_wanted.set()
                    row = await self.awake_row(pane, info, table, kids, now)
                    live[pane.key] = (pane, info, row.pop("_facts"))
                    rows.append(row)
                elif pane.key in self.acting:
                    # Mid-hibernate: the agent has exited but the record isn't written yet. Keep its row.
                    rows += [r for r in self.rows if r["id"] == pane.key]
                elif hib:
                    rows.append(await self.hibernated_row(pane, hib, now))
                else:
                    await self.undecorate(pane)
        for key in list(self.state["tabs"]):
            if key in panes:
                self.missing.pop(key, None)
                continue
            if not any(key.startswith(t.prefix + ":") for t in seen_terms):
                continue  # its terminal isn't connected right now; keep the record
            self.missing[key] = self.missing.get(key, 0) + 1
            if self.missing[key] > 3:
                log(f"tab {key} closed; forgetting hibernated session {self.state['tabs'][key]['session_id']}")
                del self.state["tabs"][key]
                save_state(self.state)
        self.live, self.panes = live, panes
        with self.snap_lock:
            self.rows = rows
        procs.forget_except(table)
        if self.state["auto"]:
            await self.auto_hibernate()

    async def awake_row(self, pane, info, table, kids, now):
        key, agent = pane.key, info.agent
        native = await pane.term.agent_status(pane) if agent.name == "claude" else {}
        status = native.get("status") if native.get("status") in ("working", "waiting", "idle") else info.status
        if status in ("working", "waiting"):
            self.busy_seen[key] = now
        last_active = max(info.status_at, pane.term.keys.get(key, 0), self.busy_seen.get(key, 0))
        facts = {
            "status": status, "transcript": info.transcript, "confident": info.confident,
            "background": agent.background(table, kids, info.pid) or bool(native.get("background_tasks")),
            "loop_tool": agent.loop_tool(info.transcript) if info.transcript else None,
            "pinned": info.session_id in self.state["pinned"], "focused": pane.focused,
            "last_active": last_active,
        }
        blocker, left = auto_decision(facts, now, self.cfg.idle_minutes)
        mem = sum(self.mem.get(p, 0) for p in [info.pid] + procs.descendants(kids, info.pid))
        prev, prev_at = self.prev_status.get(key), self.prev_status_at.get(key)
        self.prev_status[key], self.prev_status_at[key] = status, info.status_at
        # A turn that started and finished between two ticks only shows as a newer status timestamp.
        finished = status == "idle" and (prev == "working" or (
            prev is not None and prev_at is not None and info.status_at > prev_at + 0.5))
        if pane.focused or status == "working" or not self.cfg.needs_pulse or now < self.quiet_until.get(key, 0):
            self.needs.pop(key, None)
        elif status == "waiting" or finished:
            self.needs.setdefault(key, "waiting" if status == "waiting" else "done")
        if key not in self.needs and key in self.lit:
            await self.set_pulse(pane, False)
        title = self.title_for(pane, info)
        idle_min = (now - last_active) / 60 if last_active else None
        auto = self.state["auto"] and blocker is None and left is not None
        if status == "working":
            bar = "● Working"
        elif status == "waiting":
            bar = "! Waiting for you"
        elif facts["pinned"]:
            bar = "✳ Kept awake · Hibernate"
        elif auto:
            bar = f"✳ Idle {fmt_minutes(idle_min)} · sleeps in {fmt_minutes(left)} · Hibernate"
        else:
            bar = f"✳ Idle {fmt_minutes(idle_min or 0)} · Hibernate"
        await pane.term.apply(pane, Look(state=status, title=title, agent=agent.name, idle_glyph=agent.idle_glyph,
                                         sets_title=agent.sets_title, bar=bar))
        return {
            "_facts": facts, "id": key, "terminal": pane.term.kind, "agent": agent.name, "title": title,
            "state": status, "detail": native.get("detail"), "mem_mb": round(mem),
            "idle_min": round(idle_min) if idle_min is not None and status == "idle" else None,
            "sleeps_in": round(max(0, left)) if auto else None,
            "blocker": blocker if status == "idle" else None, "pinned": facts["pinned"],
            "current": pane.focused, "can_hibernate": bool(info.session_id) and status == "idle",
            "bloated": mem > self.cfg.bloat_mb, "context": self.context(agent, info.transcript),
            "needs": self.needs.get(key), "order": pane.order,
        }

    async def hibernated_row(self, pane, hib, now):
        asleep = (now - hib["hibernated_at"]) / 60
        waking = hib.get("waking_at") and now - hib["waking_at"] < 60
        if hib.get("waking_at") and not waking:
            hib.pop("waking_at")  # the resume command never started the agent; back to hibernated
            save_state(self.state)
        text = f"hibernated {fmt_minutes(asleep)} · {hib['freed_mb']} MB freed"
        bar = "… Waking" if waking else f"zz Hibernated {fmt_minutes(asleep)} · {hib['freed_mb']} MB freed · ▶ Wake"
        await pane.term.apply(pane, Look(state="waking" if waking else "hibernated", title=hib["title"],
                                         agent=hib["agent"], status_text=text, bar=bar))
        agent = BY_NAME.get(hib["agent"])
        return {"id": pane.key, "terminal": pane.term.kind, "agent": hib["agent"], "title": hib["title"],
                "state": "waking" if waking else "hibernated", "detail": None, "mem_mb": 0,
                "freed_mb": hib["freed_mb"], "asleep_min": round(asleep),
                "pinned": hib["session_id"] in self.state["pinned"], "current": False, "order": pane.order,
                "context": self.context(agent, hib.get("transcript")) if agent else {}}

    async def on_woke(self, pane, hib):
        log(f"woke {hib['session_id']} ({hib['title']})")
        self.prev_status.pop(pane.key, None)  # a resumed agent's start-up status changes aren't a finished turn
        self.prev_status_at.pop(pane.key, None)
        self.quiet_until[pane.key] = time.time() + 20
        del self.state["tabs"][pane.key]
        save_state(self.state)
        self.busy_seen[pane.key] = time.time()  # restart the idle clock

    async def undecorate(self, pane):
        self.needs.pop(pane.key, None)
        if pane.key in self.lit:
            await self.set_pulse(pane, False)
        await pane.term.apply(pane, None)

    async def set_pulse(self, pane, on):
        await pane.term.pulse(pane, on)
        (self.lit.add if on else self.lit.discard)(pane.key)

    async def auto_hibernate(self):
        for row in list(self.rows):
            if not self.state["auto"]:  # switched off mid-pass: stop now, not after the whole batch
                return
            if row.get("state") != "idle" or row.get("blocker") or row.get("sleeps_in") is None:
                continue
            if row["sleeps_in"] > 0 or row["id"] in self.acting:
                continue
            try:
                await self.hibernate(row["id"], reason="auto", strict=True)
            except ActionError as e:
                log(f"auto-hibernate skipped {row['title']}: {e}")

    # --- actions ---

    async def hibernate(self, key, reason="manual", strict=False):
        if key in self.acting:
            raise ActionError("already hibernating or waking")
        if key not in self.live:
            raise ActionError("no running agent in that tab")
        pane, info, facts = self.live[key]
        agent, term, pid = info.agent, pane.term, info.pid
        if facts["status"] != "idle":
            raise ActionError(f"{agent.label} is {facts['status']}; wait until it's idle")
        if not info.session_id:
            raise ActionError("no conversation yet, so there's nothing to resume")
        if facts["background"]:
            raise ActionError("a background job is running under it; stop it first")
        if strict and not info.confident:
            raise ActionError("not sure which session this is")
        self.acting.add(key)
        self.needs.pop(key, None)
        try:
            if key in self.lit:
                await self.set_pulse(pane, False)
            table = procs.process_table()
            tree = procs.descendants(procs.children_map(table), pid)
            parent = table.get(pid, (None,))[0]
            shell = parent if parent and is_shell(table.get(parent, (0, "", ""))[2].split(" ")[0]) else None
            starts = {p: procs.proc_start(p) for p in [pid] + tree}
            if pid not in self.mem:  # first memory sample not in yet
                self.mem = await asyncio.get_running_loop().run_in_executor(None, procs.memory_table)
            freed = round(sum(self.mem.get(p, 0) for p in [pid] + tree))
            title = self.title_for(pane, info)
            cwd = info.cwd or os.path.expanduser("~")
            await self.recheck(pane, info, table, starts[pid])  # last look, right before quitting
            await self.quit(pane, agent, pid, starts[pid])
            for p in tree:  # MCP servers normally exit with the agent; stop stragglers that are still the same process
                if procs.alive(p) and starts[p] and procs.proc_start(p) == starts[p]:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            # If the window was resized while the agent had the terminal, the shell still has the old
            # width and would draw the pre-typed command wrong; a SIGWINCH makes it re-read the size.
            if shell:
                try:
                    os.kill(shell, signal.SIGWINCH)
                except OSError:
                    pass
            lines = await self.wait_for_prompt(pane)
            sid, command = info.session_id, info.resume
            printed = agent.exit_session_id(lines)
            if printed and printed != sid:
                log(f"{agent.label} printed session {printed}, we had guessed {sid}; using the printed one")
                sid = printed
                command = agent.resume_command(self.cfg, sid, info.extra.get("argv", ()), info.extra)
            elif not printed and not info.confident:
                log(f"{agent.label} exited without printing its session and our match was a guess; "
                    f"leaving its session picker instead of a possibly wrong id")
                sid = None
                command = agent.resume_command(self.cfg, None, info.extra.get("argv", ()), info.extra)
            self.state["tabs"][key] = {
                "agent": agent.name, "session_id": sid, "cwd": cwd, "command": command,
                "config_dir": info.extra.get("config_dir"),
                "transcript": info.transcript if sid and sid == info.session_id else None,
                "title": title, "hibernated_at": time.time(), "freed_mb": freed, "reason": reason,
            }
            save_state(self.state)
            await self.leave_resume_ready(pane, agent, lines, cwd, command, freed)
            log(f"hibernated ({reason}) {agent.name} {sid} '{title}' freed ~{freed} MB")
        finally:
            self.acting.discard(key)
            self.nudge()

    async def recheck(self, pane, info, table, start):
        """Everything that must still hold at the moment of quitting: same process, same session, idle,
        nothing typed or open on screen. Statuses from the last tick can be up to 3s old."""
        agent, pid = info.agent, info.pid
        if procs.proc_start(pid) != start:
            raise ActionError(f"the {agent.label} process changed under us; try again")
        again = agent.inspect(pid, self.cfg)
        if again.session_id != info.session_id:
            raise ActionError(f"the {agent.label} session changed under us; try again")
        native = await pane.term.agent_status(pane) if agent.name == "claude" else {}
        status = native.get("status") if native.get("status") in ("working", "waiting", "idle") else again.status
        if status != "idle":
            raise ActionError(f"{agent.label} just became {status}; left it alone")
        if agent.background(table, procs.children_map(table), pid):
            raise ActionError("a background job is running under it; stop it first")
        lines, _ = await pane.term.screen(pane, drop_faint=True)
        why = agent.input_blocker(lines)
        if why:
            raise ActionError(f"not now: {why}")

    async def quit(self, pane, agent, pid, start):
        """Ask the agent to exit and wait. No SIGTERM fallback: an agent that ignores Ctrl-C may be
        mid-shutdown or showing something, so it is left running and you are told."""
        def same():
            return procs.alive(pid) and procs.proc_start(pid) == start
        if agent.quit_with == "signal":
            os.kill(pid, signal.SIGINT)
        else:
            await pane.term.send(pane, "\x03")
        for i in range(100):
            await asyncio.sleep(0.1)
            if not same():
                return
            if i == 15 and agent.quit_with == "keys":
                lines, _ = await pane.term.screen(pane)
                if any("ctrl" in l.lower() and "again" in l.lower() for l in lines[-6:]):
                    await pane.term.send(pane, "\x03")  # a version that asks for a second Ctrl-C to confirm
        raise ActionError(f"{agent.label} (pid {pid}) didn't exit after Ctrl-C; left it running")

    async def wait_for_prompt(self, pane):
        """Screen lines once the shell is back and its prompt has drawn (or after ~6s). Typing before the
        line editor is up makes it echo the text twice."""
        for _ in range(60):
            await asyncio.sleep(0.1)
            if await pane.term.shell_ready(pane):
                break
        last = None
        for _ in range(30):
            await asyncio.sleep(0.15)
            lines, cursor = await pane.term.screen(pane)
            row = lines[cursor] if 0 <= cursor < len(lines) else ""
            if row.strip() and (lines, cursor) == last:
                break
            last = (lines, cursor)
        return lines

    async def leave_resume_ready(self, pane, agent, lines, cwd, command, freed):
        """Write a banner over the agent's exit line and pre-type the resume command at the prompt."""
        if not await pane.term.shell_ready(pane):
            log("shell prompt never came back; not pre-typing the resume command")
            return
        lines, cursor = await pane.term.screen(pane)
        idx = agent.exit_line(lines)
        # Agent TUIs can exit with auto-wrap off (ESC[?7l); a long pre-typed command would then be drawn
        # clipped and redrawn below. Writing to the tty shows text without sending it to the shell.
        out = "\x1b[?7h"
        if idx is not None and cursor > idx:
            banner = f"── zz hibernated {datetime.datetime.now():%H:%M} · {freed} MB freed · Enter to wake ──"
            # Overwrite a line above the prompt rather than print below it: transient prompts (p10k) clear
            # everything under the old prompt on the next Enter.
            out += f"\x1b7\x1b[{cursor - idx}A\r\x1b[2K\x1b[38;5;141m{banner}\x1b[0m\x1b8"
        if pane.tty:
            try:
                with open(pane.tty, "w") as tty:
                    tty.write(out)
            except OSError:
                pass
        await pane.term.send(pane, f"cd {cd_arg(cwd)} && {command}")

    async def wake(self, key):
        hib = self.state["tabs"].get(key)
        pane = self.panes.get(key)
        if not hib or not pane:
            raise ActionError("that tab isn't hibernated")
        if key in self.acting or hib.get("waking_at"):
            raise ActionError("already waking")
        if not await pane.term.shell_ready(pane):
            raise ActionError("the tab is running something else; finish it first")
        hib["waking_at"] = time.time()
        save_state(self.state)
        # Ctrl-U clears whatever is on the prompt line (normally our pre-typed command).
        await pane.term.send(pane, f"\x15cd {cd_arg(hib['cwd'])} && {hib['command']}\r")
        log(f"waking {hib['agent']} {hib['session_id']} ({hib['title']})")
        self.nudge()

    async def refresh(self, key):
        """Hibernate + wake: same conversation in a fresh process, which drops any leaked memory."""
        await self.hibernate(key, reason="refresh")
        await self.wake(key)

    async def toggle(self, key):
        if key in self.state["tabs"]:
            await self.wake(key)
        elif key in self.live:
            await self.hibernate(key)
        else:
            raise ActionError("no agent in that tab")

    def set_pin(self, key, on):
        sid = self.live[key][1].session_id if key in self.live else self.state["tabs"].get(key, {}).get("session_id")
        if not sid:
            raise ActionError("no agent session in that tab")
        pins = set(self.state["pinned"])
        (pins.add if on else pins.discard)(sid)
        self.state["pinned"] = sorted(pins)
        save_state(self.state)

    def set_auto(self, on):
        self.state["auto"] = bool(on)
        save_state(self.state)
        self.nudge()

    async def focus(self, key):
        pane = self.panes.get(key)
        if not pane:
            raise ActionError("tab not found")
        await pane.term.focus(pane)

    def adopt(self, records):
        """Take over tabs another tool restored still hibernated: {pane key: record}."""
        if not isinstance(records, dict):
            raise ActionError("expected {pane key: record}")
        for key, rec in records.items():
            if not (isinstance(key, str) and isinstance(rec, dict) and isinstance(rec.get("command"), str)
                    and rec.get("agent") in BY_NAME):
                raise ActionError(f"bad record for {key!r}")
            rec.setdefault("title", (rec.get("session_id") or "session")[:8])
            rec.setdefault("cwd", os.path.expanduser("~"))
            rec.setdefault("hibernated_at", time.time())
            rec.setdefault("freed_mb", 0)
            rec.setdefault("reason", "adopted")
            rec.setdefault("session_id", None)
            self.state["tabs"][key] = rec
        save_state(self.state)
        self.nudge()

    def write_snapshot(self):
        """Every session with its resume command, from what the daemon already knows (no extra scans)."""
        rows = []
        for key, (pane, info, _facts) in self.live.items():
            if info.session_id:
                rows.append({"terminal": pane.term.kind, "tty": os.path.basename(pane.tty or ""), "pid": info.pid,
                             "agent": info.agent.name, "session_id": info.session_id, "cwd": info.cwd,
                             "command": info.resume, "title": self.title_for(pane, info), "hibernated": False})
        for key, hib in self.state["tabs"].items():
            pane = self.panes.get(key)
            rows.append({"terminal": key.split(":", 1)[0], "tty": os.path.basename((pane.tty if pane else "") or ""),
                         "pid": None, "agent": hib["agent"], "session_id": hib["session_id"], "cwd": hib["cwd"],
                         "command": hib["command"], "title": hib["title"], "hibernated": True})
        if not rows:
            return
        rows.sort(key=lambda r: r["tty"])
        os.makedirs(self.cfg.snapshot_dir, exist_ok=True)
        path = os.path.join(self.cfg.snapshot_dir, datetime.datetime.now().strftime(self.cfg.snapshot_name))
        with open(path + ".tmp", "w") as fh:
            json.dump(rows, fh, indent=1)
        os.replace(path + ".tmp", path)
        pattern = re.sub(r"%[a-zA-Z]", "*", self.cfg.snapshot_name)
        for old in sorted(glob.glob(os.path.join(self.cfg.snapshot_dir, pattern)))[:-max(1, self.cfg.snapshot_keep)]:
            os.remove(old)

    async def run_snapshots(self):
        if not self.cfg.snapshot_dir:
            return
        await asyncio.sleep(60)  # let the first ticks fill in titles
        while True:
            try:
                self.write_snapshot()
            except Exception as e:  # pylint: disable=broad-except
                log(f"snapshot failed: {type(e).__name__}: {e}")
            await asyncio.sleep(self.cfg.snapshot_minutes * 60)

    async def revert_all(self):
        for pane in self.panes.values():
            await self.undecorate(pane)
        for term in self.terminals:
            if term.up:
                await term.revert_all()

    # --- background loops ---

    async def run_ticks(self):
        self.loop = asyncio.get_running_loop()
        self.kick = asyncio.Event()
        while True:
            self.kick.clear()
            waiters, self.waiters = self.waiters, []
            try:
                await self.tick()
            except Exception as e:  # pylint: disable=broad-except
                log(f"tick failed: {type(e).__name__}: {e}\n{traceback.format_exc(limit=-3)}")
            for w in waiters:
                if not w.done():
                    w.set_result(None)
            try:
                await asyncio.wait_for(self.kick.wait(), TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def settle(self):
        """Wait for a tick that starts after now, so a snapshot taken next shows the result of an action."""
        fut = asyncio.get_running_loop().create_future()
        self.waiters.append(fut)
        self.nudge()
        try:
            await asyncio.wait_for(fut, 10)
        except asyncio.TimeoutError:
            pass

    def nudge(self):
        """Tick now rather than in up to 3s (callable from the HTTP threads too)."""
        if self.kick:
            self.loop.call_soon_threadsafe(self.kick.set)

    async def run_mem(self):
        loop = asyncio.get_running_loop()
        self.mem_wanted = asyncio.Event()
        while True:
            self.mem_wanted.clear()
            self.mem = await loop.run_in_executor(None, procs.memory_table)
            try:
                await asyncio.wait_for(self.mem_wanted.wait(), MEM_SECONDS)
                await asyncio.sleep(5)  # let a just-started agent load before measuring it
            except asyncio.TimeoutError:
                pass

    async def run_pulse(self):
        """Blink tabs that need you, until you open them."""
        lit = False
        while True:
            lit = not lit
            for key in list(self.needs):
                pane = self.panes.get(key)
                if not pane or key in self.acting:
                    continue
                try:
                    await self.set_pulse(pane, lit)
                except Exception:  # pylint: disable=broad-except
                    pass
            await asyncio.sleep(PULSE_SECONDS)

    def snapshot(self):
        with self.snap_lock:
            rank = {"working": 0, "waiting": 1, "idle": 2, "waking": 3, "hibernated": 4}
            rows = sorted(self.rows, key=lambda r: (rank.get(r["state"], 2), r["order"]))  # awake on top
        awake = [r for r in rows if r["state"] not in ("hibernated", "waking")]
        return {
            "auto": self.state["auto"], "idle_minutes": self.cfg.idle_minutes, "sessions": rows,
            "awake": len(awake), "asleep": len(rows) - len(awake),
            "in_use_mb": sum(r["mem_mb"] for r in awake),
            "freed_mb": sum(r.get("freed_mb") or 0 for r in rows if r["state"] == "hibernated"),
            "terminals": [t.kind for t in self.terminals if t.up], "title": self.cfg.panel_title,
        }


__all__ = ["Engine", "ActionError", "STATE_DIR", "log", "fmt_minutes", "strip_glyph", "auto_decision"]
