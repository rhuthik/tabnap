"""`tabnap top`: the session list in any terminal (and in a tmux popup)."""
import curses
import time

from tabnap.engine import fmt_minutes
from tabnap.server import api

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
LABEL = {"claude": "Claude", "codex": "Codex"}
HELP = "↑↓ select   h hibernate   H all idle   w wake   r refresh   p pin   a auto   ⏎ go to tab   q quit"


def gb(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{round(mb)} MB"


def describe(r):
    if r.get("needs") == "done":
        return f"finished · needs you · {r['mem_mb']} MB"
    if r["state"] == "hibernated":
        return f"hibernated {fmt_minutes(r['asleep_min'])} · {r['freed_mb']} MB freed"
    if r["state"] == "waking":
        return "waking…"
    if r["state"] in ("working", "waiting"):
        return f"{r.get('detail') or r['state']} · {r['mem_mb']} MB"
    s = f"idle {fmt_minutes(r.get('idle_min') or 0)} · {gb(r['mem_mb'])}"
    if r.get("bloated"):
        s += " · refresh?"
    if r.get("pinned"):
        s += " · kept awake"
    elif r.get("sleeps_in") is not None:
        s += f" · sleeps in {fmt_minutes(r['sleeps_in'])}"
    elif r.get("blocker") and r["blocker"] != "tab in focus":
        s += f" · {r['blocker']}"
    return s


def glyph(r, frame):
    return {"working": SPINNER[frame % len(SPINNER)], "waiting": "!", "hibernated": "z", "waking": "…"}.get(r["state"], "✳")


def draw(scr, st, sel, frame, msg):
    scr.erase()
    h, w = scr.getmaxyx()

    def put(y, x, text, attr=0):
        if 0 <= y < h and x < w:
            try:
                scr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    head = (f" {st.get('title') or 'tabnap'}   {st['awake']} awake · {st['asleep']} asleep · {gb(st['in_use_mb'])} in use · "
            f"{gb(st['freed_mb'])} freed")
    put(0, 0, head, curses.A_BOLD)
    auto = f"auto-hibernate {'on' if st['auto'] else 'off'} ({st['idle_minutes']:g}m) "
    put(0, max(len(head) + 2, w - len(auto) - 1), auto, curses.color_pair(3) if st["auto"] else curses.A_DIM)
    put(1, 0, "─" * (w - 1), curses.A_DIM)
    y = 2
    for i, r in enumerate(st["sessions"]):
        if y >= h - 2:
            break
        color = {"working": 1, "waiting": 4, "hibernated": 2, "waking": 2}.get(r["state"], 0)
        attr = curses.color_pair(color)
        if r.get("needs"):
            attr = curses.color_pair(4) | (curses.A_BOLD if frame % 4 < 2 else 0)
        marker = "▶" if i == sel else " "
        put(y, 0, marker, curses.A_BOLD)
        put(y, 2, glyph(r, frame), attr | curses.A_BOLD)
        put(y, 4, f"{LABEL.get(r['agent'], r['agent']):<6}", curses.A_DIM)
        title_w = max(10, w - 52)
        title = r["title"][:title_w]
        put(y, 11, title, (curses.A_REVERSE if i == sel else 0) | (curses.color_pair(2) if color == 2 else 0))
        put(y, 12 + title_w, describe(r), attr)
        y += 1
        if i == sel:
            ctx = r.get("context") or {}
            for who, text in (("You", ctx.get("prompt")), (LABEL.get(r["agent"], "Agent"), ctx.get("answer"))):
                if text and y < h - 2:
                    put(y, 11, f"{who}: ", curses.A_BOLD | curses.A_DIM)
                    put(y, 11 + len(who) + 2, text, curses.A_DIM)
                    y += 1
    if not st["sessions"]:
        hint = ("No Claude Code or Codex sessions found." if st.get("terminals")
                else "Not connected to iTerm2 or tmux yet.")
        put(3, 2, hint + "  `tabnap doctor` shows what tabnap can see.", curses.A_DIM)
    put(h - 2, 0, "─" * (w - 1), curses.A_DIM)
    put(h - 1, 1, msg or HELP, curses.color_pair(4) if msg else curses.A_DIM)
    scr.refresh()


def loop(scr):
    curses.curs_set(0)
    curses.use_default_colors()
    for n, c in ((1, curses.COLOR_YELLOW), (2, curses.COLOR_MAGENTA), (3, curses.COLOR_GREEN), (4, curses.COLOR_RED)):
        curses.init_pair(n, c, -1)
    scr.timeout(150)
    sel, sel_id, frame, msg, msg_until, st, fetched = 0, None, 0, "", 0, None, 0
    while True:
        now = time.time()
        if now - fetched > 1 or st is None:
            try:
                st = api("/api/state", method="GET")
            except SystemExit as e:
                st = {"awake": 0, "asleep": 0, "in_use_mb": 0, "freed_mb": 0, "auto": False, "idle_minutes": 0,
                      "sessions": []}
                msg, msg_until = str(e), now + 2
            fetched = now
        rows = st["sessions"]
        ids = [r["id"] for r in rows]
        sel = ids.index(sel_id) if sel_id in ids else min(sel, max(0, len(rows) - 1))  # follow it when the list re-sorts
        sel_id = ids[sel] if rows else None
        frame += 1
        draw(scr, st, sel, frame, msg if now < msg_until else "")
        ch = scr.getch()
        if ch in (ord("q"), 27):
            return
        if ch in (curses.KEY_DOWN, ord("j")) and rows:
            sel = min(sel + 1, len(rows) - 1)
            sel_id = rows[sel]["id"]
        elif ch in (curses.KEY_UP, ord("k")) and rows:
            sel = max(sel - 1, 0)
            sel_id = rows[sel]["id"]
        elif ch == ord("H"):
            draw(scr, st, sel, frame, "putting idle sessions to sleep…")
            try:
                res = api("/api/hibernate_idle")
                msg, msg_until = f"{len(res['slept'])} put to sleep, {len(res['skipped'])} left awake", time.time() + 4
            except SystemExit as e:
                msg, msg_until = str(e), time.time() + 4
            fetched = 0
        elif ch == ord("a") or (rows and ch in (ord("h"), ord("w"), ord("r"), ord("p"), 10, 13, curses.KEY_ENTER)):
            r = rows[sel] if rows else None
            params = {}
            if ch == ord("a"):
                path, params = "/api/auto", {"on": "0" if st["auto"] else "1"}
            elif ch == ord("h"):
                path = "/api/hibernate"
            elif ch == ord("w"):
                path = "/api/wake"
            elif ch == ord("r"):
                path = "/api/refresh"
            elif ch == ord("p"):
                path = "/api/unpin" if r.get("pinned") else "/api/pin"
            else:
                path = "/api/focus"
            if r and "id" not in params and path != "/api/auto":
                params["id"] = r["id"]
            msg, msg_until = ("working…", now + 30) if path in ("/api/hibernate", "/api/wake", "/api/refresh") else ("", 0)
            draw(scr, st, sel, frame, msg)
            try:
                api(path, **params)
                msg, msg_until = "", 0
            except SystemExit as e:
                msg, msg_until = str(e), time.time() + 4
            fetched = 0
            if path == "/api/focus":
                return  # jumped to the tab; close (handy in a tmux popup)


def main():
    curses.wrapper(loop)
