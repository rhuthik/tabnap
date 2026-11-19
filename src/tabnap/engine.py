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

from tabnap import groups, procs
from tabnap.agents import BY_NAME, top_level
from tabnap.config import STATE_DIR, STATE_FILE
from tabnap.terminals.base import PLACEHOLDER_TITLES, Look, is_shell

TICK_SECONDS = 3
MEM_SECONDS = 60
PULSE_SECONDS = 0.8
SUGGEST_SECONDS = 20
GROUP_SETTLE_SECONDS = 15  # iTerm2 takes a moment to report a tab we just filed as grouped
GROUP_FORGET_DAYS = 60     # forget the group of a conversation not seen for this long
CAP_MIN_IDLE = 5  # minutes a session must be idle before max_awake may put it to sleep
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


CONNECT_TIMEOUT = 20  # seconds a terminal gets to connect before we retry later


def auto_decision(f, now, cfg):
    """(blocker, minutes_left). blocker is a reason auto-hibernate won't run; minutes_left <= 0 means due."""
    if f["pinned"]:
        return "kept awake", None
    if f["status"] in ("working", "waiting"):
        return f["status"], None
    if f["agent"] not in cfg.auto_agents:
        return f"auto is off for {f['agent']}", None
    if cfg.skipped_dir(f["cwd"]):
        return "folder skipped by auto", None
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
    return None, cfg.idle_minutes - (now - f["last_active"]) / 60


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
    st.setdefault("groups", {})    # agent session id -> {group, pane, at}: its tab group, to restore it
    st.setdefault("suggested", {}) # agent session id -> {group, answer}: Claude's group pick and your answer
    st.setdefault("keep", {})      # agent session id -> don't ask "still need this tab?" before this time
    st.setdefault("closed", [])    # hibernated tabs closed from the panel, with their resume commands
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
        self.grouped = set()      # pane keys without a conversation yet that a rule already filed
        self.collapse_want = {}   # (terminal, group id) -> collapsed state tabnap last wanted
        self.asks = []            # questions for the panel, rebuilt every tick
        self.lit_groups = {}      # (terminal, group id) -> name, for collapsed groups blinking for a tab inside

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
                # A terminal that is still starting up can accept the socket and never answer; without a
                # timeout that stalls every tick (and so every reconnect) for good.
                term.up = await asyncio.wait_for(term.start(self), timeout=CONNECT_TIMEOUT)
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
        existing = set(panes)
        for term in seen_terms:
            existing |= term.all_keys or set()
        for key in list(self.state["tabs"]):
            if key in existing:
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
        try:
            await self.sync_groups(panes, rows)
        except Exception as e:  # pylint: disable=broad-except
            log(f"tab groups: {type(e).__name__}: {e}")
        self.asks = self.build_asks(rows)
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
            "last_active": last_active, "agent": agent.name, "cwd": info.cwd,
        }
        blocker, left = auto_decision(facts, now, self.cfg)
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
            "needs": self.needs.get(key), "order": pane.order, "cwd": info.cwd, "session_id": info.session_id,
            "group": self.group_for(pane, title, info.cwd, info.session_id),
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
                "cwd": hib["cwd"], "session_id": hib["session_id"],
                "group": self.group_for(pane, hib["title"], hib["cwd"], hib["session_id"]),
                "context": self.context(agent, hib.get("transcript")) if agent else {}}

    def group_for(self, pane, title, cwd, sid=None):
        """The tab's own group; else the group tabnap remembers for this conversation; else the first rule."""
        if pane.group:
            return pane.group
        if sid and pane.term.groups_supported() and sid in self.state["groups"]:
            return self.state["groups"][sid].get("group", "")
        return groups.match(groups.compile_rules(self.cfg.groups), title, cwd)

    async def sync_groups(self, panes, rows):
        """Keep tab groups in step with what tabnap knows:
        - remember each conversation's group (state["groups"]: session id -> {group, pane, at});
        - put a conversation back in its group when it shows up in a new tab (after a reboot or restore);
        - file conversations seen for the first time into the group a rule names;
        - collapse groups whose tabs are all asleep."""
        now = time.time()
        mem = self.state["groups"]
        rules = groups.compile_rules(self.cfg.groups)
        todo, changed = {}, False
        for r in rows:
            pane, sid = panes.get(r["id"]), r.get("session_id")
            if not pane or not pane.term.groups_supported():
                continue
            if not sid:  # no conversation yet (or resumed through a picker): rules only, remembered until restart
                if r.get("group") and not pane.group and pane.key not in self.grouped:
                    todo.setdefault((pane.term, r["group"]), []).append(pane)
                    self.grouped.add(pane.key)
                continue
            m = mem.get(sid)
            if pane.group:
                if not m or m.get("group") != pane.group or m.get("pane") != pane.key:
                    mem[sid] = {"group": pane.group, "pane": pane.key, "at": now}
                    changed = True
                continue
            if m is None:
                name = groups.match(rules, r["title"], r.get("cwd"))
                if name:
                    todo.setdefault((pane.term, name), []).append(pane)
                    mem[sid] = {"group": name, "pane": pane.key, "at": now}
                    changed = True
            elif not m.get("group"):
                continue  # you took it out of its group; leave it out
            elif m.get("pane") != pane.key:  # same conversation, new tab: put it back
                todo.setdefault((pane.term, m["group"]), []).append(pane)
                m.update(pane=pane.key, at=now)
                changed = True
            elif now - m.get("at", 0) > GROUP_SETTLE_SECONDS:  # was in the group in this tab, isn't any more
                m.update(group="", at=now)
                changed = True
        seen = {r.get("session_id") for r in rows}
        for sid in [s for s, m in mem.items() if s not in seen and now - m.get("at", 0) > GROUP_FORGET_DAYS * 86400]:
            del mem[sid]
            changed = True
        if changed:
            save_state(self.state)
        for (term, name), members in todo.items():
            await term.set_group(members, name)
            log(f"filed {len(members)} tab(s) into group {name}")
        if not self.cfg.collapse_groups:
            return
        by_id = {r["id"]: r for r in rows}
        members = {}
        for pane in panes.values():
            if pane.group_id:
                row = by_id.get(pane.key) or {}
                members.setdefault((pane.term, pane.group_id), []).append(
                    {"state": row.get("state", "none"), "current": pane.focused})
        for gkey, ms in members.items():
            want = groups.want_collapsed(ms)
            before = self.collapse_want.get(gkey)
            self.collapse_want[gkey] = want
            # Act on changes only, so a group you open or close by hand stays that way; and never
            # expand at start-up, when we don't know what you left collapsed on purpose.
            if want != before and (want or before is not None):
                await gkey[0].set_collapsed(gkey[1], want)

    async def assign_groups(self, mapping):
        """File tabs into groups by hand: {group name: [pane keys]}. Moves a tab out of any group it's in."""
        if not isinstance(mapping, dict) or not all(isinstance(v, list) for v in mapping.values()):
            raise ActionError("expected {group: [tab ids]}")
        sids = {r["id"]: r.get("session_id") for r in self.rows}
        for name, keys in mapping.items():
            missing = [k for k in keys if k not in self.panes]
            if missing:
                raise ActionError(f"tab not found: {missing[0]}")
            by_term = {}
            for k in keys:
                by_term.setdefault(self.panes[k].term, []).append(self.panes[k])
            for term, panes in by_term.items():  # one call per group, so a new group is created once
                await term.set_group(panes, str(name))
            self.grouped.update(keys)
            for k in keys:
                if sids.get(k):
                    self.state["groups"][sids[k]] = {"group": str(name), "pane": k, "at": time.time()}
            log(f"filed {len(keys)} tab(s) into group {name} by hand")
        save_state(self.state)
        self.nudge()

    # --- questions in the panel (nothing happens until you answer) ---

    def build_asks(self, rows):
        """Questions for the panel: "put this tab in group X?" and "still need this tab?"."""
        now, asks = time.time(), []
        for r in rows:
            sid = r.get("session_id")
            s = self.state["suggested"].get(sid) if sid else None
            if s and s.get("group") and s.get("answer") is None and not r.get("group"):
                asks.append({"id": f"group:{sid}", "kind": "group", "tab": r["id"], "title": r["title"],
                             "group": s["group"], "text": f"Put this session in {s['group']}?",
                             "choices": [["yes", f"Move to {s['group']}"], ["no", "No"]]})
        if self.cfg.stale_days:
            stale = [r for r in rows if r["state"] == "hibernated" and r.get("session_id")
                     and r.get("asleep_min", 0) >= self.cfg.stale_days * 1440
                     and self.state["keep"].get(r["session_id"], 0) < now]
            for r in sorted(stale, key=lambda r: -r["asleep_min"]):
                asks.append({"id": f"stale:{r['session_id']}", "kind": "stale", "tab": r["id"], "title": r["title"],
                             "text": f"Asleep for {fmt_minutes(r['asleep_min'])}. Still need this tab?",
                             "choices": [["keep", "Keep"], ["close", "Close tab"]]})
        return asks

    async def answer(self, ask_id, choice):
        ask = next((a for a in self.asks if a["id"] == ask_id), None)
        if not ask or choice not in [c[0] for c in ask["choices"]]:
            raise ActionError("that question is gone; refresh")
        sid = ask_id.split(":", 1)[1]
        if ask["kind"] == "group":
            self.state["suggested"][sid]["answer"] = choice
            if choice == "yes":
                await self.assign_groups({ask["group"]: [ask["tab"]]})
        elif choice == "keep":
            self.state["keep"][sid] = time.time() + self.cfg.stale_days * 86400  # ask again after another stretch
        else:
            await self.close_tab(ask["tab"])
        save_state(self.state)
        self.asks = [a for a in self.asks if a["id"] != ask_id]
        self.nudge()

    async def close_tab(self, key):
        """Close a hibernated tab, keeping its resume command in state["closed"] (newest last, 100 kept)."""
        hib, pane = self.state["tabs"].get(key), self.panes.get(key)
        if not hib or not pane:
            raise ActionError("only a hibernated tab can be closed from here")
        await pane.term.close(pane)
        self.state["closed"] = (self.state["closed"] + [{**hib, "closed_at": time.time()}])[-100:]
        del self.state["tabs"][key]
        save_state(self.state)
        log(f"closed tab of {hib['session_id']} ({hib['title']}); resume: {hib['command']}")

    # --- "which group does this belong in?" (off unless features.group_suggestions) ---

    def suggestion_candidates(self):
        """Sessions to ask Claude about: in a terminal with groups, not in one, never decided before."""
        names = {r.get("group") for r in self.rows if r.get("group")}
        if not names:
            return []
        out = []
        for r in self.rows:
            sid, pane = r.get("session_id"), self.panes.get(r["id"])
            if (sid and pane and pane.term.groups_supported() and not r.get("group") and sid not in self.state["groups"]
                    and sid not in self.state["suggested"] and r["title"] not in PLACEHOLDER_TITLES):
                out.append(r)
        return out

    async def run_suggestions(self):
        while True:
            await asyncio.sleep(SUGGEST_SECONDS)
            if not self.cfg.group_suggestions:
                continue
            for r in self.suggestion_candidates()[:1]:  # one at a time: each is a model call
                sid = r["session_id"]
                try:
                    name = await asyncio.get_running_loop().run_in_executor(None, self.classify, r)
                except Exception as e:  # pylint: disable=broad-except
                    log(f"group suggestion for '{r['title']}' failed: {type(e).__name__}: {e}")
                    name = ""
                self.state["suggested"][sid] = {"group": name, "answer": None, "at": time.time()}
                save_state(self.state)
                if name:
                    log(f"suggested group {name} for '{r['title']}'")
                    self.nudge()

    def classify(self, row):
        members = {}
        for r in self.rows:
            if r.get("group"):
                members.setdefault(r["group"], []).append(r["title"])
        key = row["id"]
        transcript, agent, config_dir = None, BY_NAME.get(row["agent"]), None
        if key in self.live:
            info = self.live[key][1]
            transcript, config_dir = info.transcript, info.extra.get("config_dir")
        elif key in self.state["tabs"]:
            transcript, config_dir = self.state["tabs"][key].get("transcript"), self.state["tabs"][key].get("config_dir")
        prompt = ((agent.context(transcript) if agent and transcript else {}) or {}).get("prompt", "")
        return groups.classify(self.cfg.suggest_command, members, row["title"], row.get("cwd"), prompt, config_dir)

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
        for row in self.over_cap():
            if not self.state["auto"]:
                return
            try:
                await self.hibernate(row["id"], reason=f"auto (more than {self.cfg.max_awake} awake)", strict=True)
            except ActionError as e:
                log(f"auto-hibernate skipped {row['title']}: {e}")

    def over_cap(self):
        """Idle sessions to put to sleep so no more than max_awake stay awake: the longest-idle first, idle
        at least CAP_MIN_IDLE minutes, and only ones auto-hibernate could take anyway (same rules)."""
        if self.cfg.max_awake <= 0:
            return []
        awake = [r for r in self.rows if r.get("state") in ("working", "waiting", "idle") and r["id"] not in self.acting]
        excess = len(awake) - self.cfg.max_awake
        if excess <= 0:
            return []
        candidates = [r for r in awake if r["state"] == "idle" and not r.get("blocker")
                      and (r.get("idle_min") or 0) >= CAP_MIN_IDLE]
        candidates.sort(key=lambda r: -(r.get("idle_min") or 0))
        return candidates[:excess]

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
        await self.hibernated_row(pane, hib, time.time())  # waking look first: it must land before the agent starts
        # Ctrl-U clears whatever is on the prompt line (normally our pre-typed command).
        await pane.term.send(pane, f"\x15cd {cd_arg(hib['cwd'])} && {hib['command']}\r")
        log(f"waking {hib['agent']} {hib['session_id']} ({hib['title']})")
        self.nudge()

    async def hibernate_idle(self, group=None):
        """Put every idle session to sleep that auto-hibernate could take (same rules, no idle timer),
        only those in `group` if given. Returns (slept titles, {title: why skipped})."""
        slept, skipped = [], {}
        for row in [r for r in self.rows if r.get("state") == "idle" and (group is None or r.get("group") == group)]:
            if row.get("blocker"):
                skipped[row["title"]] = row["blocker"]
                continue
            try:
                await self.hibernate(row["id"], reason="sleep all idle", strict=True)
                slept.append(row["title"])
            except ActionError as e:
                skipped[row["title"]] = str(e)
        return slept, skipped

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
        os.makedirs(self.cfg.snapshot_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.cfg.snapshot_dir, datetime.datetime.now().strftime(self.cfg.snapshot_name))
        # Private: snapshots hold session ids, folders and resume commands.
        with open(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
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
            await asyncio.sleep(max(1.0, self.cfg.snapshot_minutes) * 60)

    async def reload(self):
        """Re-read the config file and apply it now. Returns the changed keys that need a restart."""
        from tabnap import config
        new = config.load()
        changed = [st for st in config.SETTINGS if getattr(new, st.attr) != getattr(self.cfg, st.attr)]
        for st in changed:
            if not st.restart:
                setattr(self.cfg, st.attr, getattr(new, st.attr))  # terminals share this object, so they see it too
        if not self.cfg.context:
            self.cache = {k: v for k, v in self.cache.items() if k[0] != "ctx"}
        for term in self.terminals:
            if term.up:
                await term.on_config(self.cfg, [st.key for st in changed])
        self.nudge()
        if changed:
            log(f"config reloaded: {', '.join(st.key for st in changed)}")
        return [st.key for st in changed if st.restart]

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
            hidden = {}  # collapsed groups hiding a tab that needs you: blink the group instead
            for key in list(self.needs):
                pane = self.panes.get(key)
                if not pane or key in self.acting:
                    continue
                if pane.group_id and pane.group_collapsed:
                    hidden[(pane.term, pane.group_id)] = pane.group
                try:
                    await self.set_pulse(pane, lit)
                except Exception:  # pylint: disable=broad-except
                    pass
            for gkey, name in list(hidden.items()) + [(g, n) for g, n in self.lit_groups.items() if g not in hidden]:
                try:
                    await gkey[0].pulse_group(gkey[1], name, lit and gkey in hidden)
                except Exception:  # pylint: disable=broad-except
                    pass
            self.lit_groups = hidden
            await asyncio.sleep(PULSE_SECONDS)

    def snapshot(self):
        with self.snap_lock:
            rank = {"working": 0, "waiting": 1, "idle": 2, "waking": 3, "hibernated": 4}
            names = list(self.cfg.groups)
            for r in sorted(self.rows, key=lambda r: r["order"]):
                if r.get("group") and r["group"] not in names:
                    names.append(r["group"])
            pos = {n: i for i, n in enumerate(names)}
            # Grouped sessions first, group by group; ungrouped ones last. Awake on top within each.
            rows = sorted(self.rows, key=lambda r: (pos.get(r.get("group"), len(names)), rank.get(r["state"], 2), r["order"]))
        awake = [r for r in rows if r["state"] not in ("hibernated", "waking")]
        return {
            "auto": self.state["auto"], "idle_minutes": self.cfg.idle_minutes, "sessions": rows,
            "awake": len(awake), "asleep": len(rows) - len(awake),
            "in_use_mb": sum(r["mem_mb"] for r in awake),
            "freed_mb": sum(r.get("freed_mb") or 0 for r in rows if r["state"] == "hibernated"),
            "terminals": [t.kind for t in self.terminals if t.up], "title": self.cfg.panel_title,
            "groups": [n for n in names if any(r.get("group") == n for r in rows)], "asks": self.asks,
        }


__all__ = ["Engine", "ActionError", "STATE_DIR", "log", "fmt_minutes", "strip_glyph", "auto_decision"]
