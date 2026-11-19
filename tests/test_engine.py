import asyncio
import os
import tempfile
import unittest
from unittest import mock

from tabnap import procs
from tabnap.agents.base import Agent, Live
from tabnap.config import Config
from tabnap.engine import ActionError, Engine
from tabnap.service import systemd_quote
from tabnap.terminals.base import Pane, Terminal

RULE = "─" * 20


def setUpModule():
    # Never touch the real state file: point it at a temp dir for the whole module.
    global _patch
    _patch = mock.patch("tabnap.engine.STATE_FILE", os.path.join(tempfile.mkdtemp(), "state.json"))
    _patch.start()


def tearDownModule():
    _patch.stop()


class FakeAgent(Agent):
    name, label, quit_with = "fake", "Fake", "signal"

    def __init__(self, status_now="idle", screen_blocker=None):
        self.status_now = status_now
        self.screen_blocker = screen_blocker

    def inspect(self, pid, cfg, claimed=frozenset()):
        return Live(self, pid, session_id="s1", transcript="/t", status=self.status_now)

    def input_blocker(self, lines):
        return self.screen_blocker


class FakeTerm(Terminal):
    kind = "fake"

    def __init__(self):
        super().__init__(Config())
        self.sent = []

    async def screen(self, pane, drop_faint=False):
        return [RULE, "> ", RULE], 1

    async def send(self, pane, text):
        self.sent.append(text)


def engine_with(agent, background=False):
    e = Engine(Config(), [FakeTerm()])
    term = e.terminals[0]
    pane = Pane(key="fake:1", tty=None, name="t", focused=False, shell=False, order=(), term=term)
    pid = os.getpid()  # a real, live process that must never be signalled by these tests
    info = Live(agent, pid, session_id="s1", transcript="/t", status="idle")
    facts = {"status": "idle", "background": background}
    e.live[pane.key] = (pane, info, facts)
    e.panes[pane.key] = pane
    e.mem = {pid: 100}
    return e, term


class HibernateSafety(unittest.TestCase):
    def run_hibernate(self, agent, background=False):
        e, term = engine_with(agent, background)
        with mock.patch("os.kill") as kill:
            with self.assertRaises(ActionError) as err:
                asyncio.run(e.hibernate("fake:1"))
            kill.assert_not_called()
        self.assertEqual(term.sent, [])
        self.assertNotIn("fake:1", e.acting)
        return str(err.exception)

    def test_turned_busy_between_tick_and_quit(self):
        self.assertIn("just became working", self.run_hibernate(FakeAgent(status_now="working")))

    def test_any_screen_blocker_stops_it(self):
        self.assertIn("dialog or menu open", self.run_hibernate(FakeAgent(screen_blocker="dialog or menu open")))

    def test_background_job_stops_it(self):
        self.assertIn("background job", self.run_hibernate(FakeAgent(), background=True))


class AdoptAndSnapshot(unittest.TestCase):
    def test_adopt_validates_and_fills_defaults(self):
        e, _ = engine_with(FakeAgent())
        with mock.patch("tabnap.engine.save_state"):
            e.adopt({"iterm:abc": {"agent": "claude", "session_id": "s9", "command": "claude --resume s9"}})
            with self.assertRaises(ActionError):
                e.adopt({"iterm:x": {"agent": "nope", "command": "rm -rf /"}})
        rec = e.state["tabs"]["iterm:abc"]
        self.assertEqual((rec["title"], rec["reason"], rec["freed_mb"]), ("s9", "adopted", 0))
        self.assertNotIn("iterm:x", e.state["tabs"])

    def test_snapshot_writes_and_prunes(self):
        e, _ = engine_with(FakeAgent())
        e.cfg.snapshot_dir = tempfile.mkdtemp()
        e.cfg.snapshot_keep = 2
        for n in ("snap-20260101-0000.json", "snap-20260101-0001.json", "snap-20260101-0002.json"):
            open(os.path.join(e.cfg.snapshot_dir, n), "w").write("[]")
        e.cfg.snapshot_name = "snap-%Y%m%d-%H%M.json"
        e.state["tabs"]["fake:2"] = {"agent": "claude", "session_id": "s2", "cwd": "/w", "command": "claude --resume s2",
                                     "title": "Asleep"}
        e.write_snapshot()
        files = sorted(os.listdir(e.cfg.snapshot_dir))
        self.assertEqual(len(files), 2)
        import json
        newest = os.path.join(e.cfg.snapshot_dir, files[-1])
        self.assertEqual(os.stat(newest).st_mode & 0o777, 0o600)
        rows = json.load(open(newest))
        self.assertEqual({r["session_id"] for r in rows}, {"s1", "s2"})


class AwakeCap(unittest.TestCase):
    def test_longest_idle_go_first_and_blocked_ones_stay(self):
        e, _ = engine_with(FakeAgent())
        e.cfg.max_awake = 2
        e.rows = [
            {"id": "a", "state": "working", "idle_min": None},
            {"id": "b", "state": "idle", "idle_min": 5},
            {"id": "c", "state": "idle", "idle_min": 50},
            {"id": "d", "state": "idle", "idle_min": 90, "blocker": "kept awake"},
            {"id": "e", "state": "idle", "idle_min": 20},
            {"id": "z", "state": "hibernated"},
        ]
        self.assertEqual([r["id"] for r in e.over_cap()], ["c", "e", "b"][:3])
        e.cfg.max_awake = 4
        self.assertEqual([r["id"] for r in e.over_cap()], ["c"])
        e.cfg.max_awake = 0
        self.assertEqual(e.over_cap(), [])


class SleepAllIdle(unittest.TestCase):
    def test_only_unblocked_idle_rows(self):
        e, _ = engine_with(FakeAgent())
        e.rows = [{"id": "a", "title": "A", "state": "idle"}, {"id": "b", "title": "B", "state": "idle", "blocker": "kept awake"},
                  {"id": "c", "title": "C", "state": "working"}]
        tried = []

        async def fake_hibernate(key, reason="manual", strict=False):
            tried.append((key, strict))
        e.hibernate = fake_hibernate
        slept, skipped = asyncio.run(e.hibernate_idle())
        self.assertEqual((slept, skipped, tried), (["A"], {"B": "kept awake"}, [("a", True)]))


class ConfigFile(unittest.TestCase):
    def test_set_value_round_trips_and_validates(self):
        from tabnap import config
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        config.set_value("auto.max_awake", "6", path)
        config.set_value("auto.agents", "claude", path)
        config.set_value("features.context", "on", path)
        config.set_value("claude.resume_by_dir", '{"~/p/*": "claude-{profile} --resume {id}"}', path)
        c = config.load(path)
        self.assertEqual((c.max_awake, c.auto_agents, c.context), (6, ["claude"], True))
        self.assertEqual(c.claude_resume_by_dir, {"~/p/*": "claude-{profile} --resume {id}"})
        for key, bad in (("idle_minutes", "0"), ("auto.agents", "claude,gemini"), ("features.context", "maybe"),
                         ("snapshot.every_minutes", "0")):
            with self.assertRaises(ValueError):
                config.set_value(key, bad, path)
        with self.assertRaises(KeyError):
            config.set_value("nope", "1", path)


class Connect(unittest.TestCase):
    def test_hanging_terminal_start_times_out_and_retries_later(self):
        class Hangs(FakeTerm):
            async def start(self, engine):
                await asyncio.sleep(3600)
        e = Engine(Config(), [Hangs()])
        with mock.patch("tabnap.engine.CONNECT_TIMEOUT", 0.05), mock.patch("tabnap.engine.log"):
            asyncio.run(asyncio.wait_for(e.connect(), 2))
        term = e.terminals[0]
        self.assertFalse(term.up)
        self.assertIn(term, e.retry_at)


class ProcessIdentity(unittest.TestCase):
    def test_identity_is_stable_and_specific(self):
        me = procs.proc_start(os.getpid())
        self.assertTrue(me)
        self.assertEqual(me, procs.proc_start(os.getpid()))
        self.assertEqual(procs.proc_start(999999), "")


class Service(unittest.TestCase):
    def test_systemd_quote(self):
        self.assertEqual(systemd_quote('/opt/my apps/py "x" 100%$HOME'), '"/opt/my apps/py \\"x\\" 100%%$$HOME"')



class GroupTerm(FakeTerm):
    def __init__(self):
        super().__init__()
        self.filed, self.collapsed = [], []

    async def set_group(self, panes, name):
        self.filed.append((name, sorted(p.key for p in panes)))

    async def set_collapsed(self, group_id, collapsed):
        self.collapsed.append((group_id, collapsed))

    def groups_supported(self):
        return True

    async def close(self, pane):
        self.closed = getattr(self, "closed", []) + [pane.key]


class TabGroups(unittest.TestCase):
    def setUp(self):
        cfg = Config()
        cfg.groups = {"oncall": r"CSTFP-\d+"}
        self.e = Engine(cfg, [GroupTerm()])
        self.term = self.e.terminals[0]
        self.e.state.update(groups={}, suggested={}, keep={}, closed=[], tabs={})  # the module shares one state file

    def pane(self, key, group="", gid="", focused=False):
        return Pane(key=key, tty=None, name="", focused=focused, shell=False, order=(), term=self.term,
                    group=group, group_id=gid)

    def test_files_matching_ungrouped_tabs_once(self):
        panes = {k: self.pane(k) for k in ("fake:1", "fake:2", "fake:3")}
        panes["fake:3"].group = "mine"
        rows = [{"id": "fake:1", "state": "idle", "group": "oncall", "title": "CSTFP-1 x", "session_id": "s1"},
                {"id": "fake:2", "state": "idle", "group": "", "title": "other", "session_id": "s2"},
                {"id": "fake:3", "state": "idle", "group": "mine", "title": "CSTFP-3", "session_id": "s3"}]
        asyncio.run(self.e.sync_groups(panes, rows))
        asyncio.run(self.e.sync_groups(panes, rows))  # pane.group not refreshed yet: still not filed twice
        self.assertEqual(self.term.filed, [("oncall", ["fake:1"])])
        self.assertEqual(self.e.state["groups"]["s3"]["group"], "mine")
        self.assertEqual(self.e.group_for(self.pane("x"), "zz CSTFP-1821 Jira", "/tmp"), "oncall")
        self.assertEqual(self.e.group_for(self.pane("x", group="mine"), "CSTFP-1", "/tmp"), "mine")

    def test_collapse_on_changes_only(self):
        panes = {k: self.pane(k, "oncall", "g1") for k in ("fake:1", "fake:2")}

        def tick(*states):
            rows = [{"id": k, "state": st, "group": "oncall"} for k, st in zip(panes, states)]
            asyncio.run(self.e.sync_groups(panes, rows))
        tick("idle", "hibernated")        # start-up with an awake tab: leave the group as you left it
        self.assertEqual(self.term.collapsed, [])
        tick("hibernated", "hibernated")  # last one fell asleep
        tick("hibernated", "hibernated")  # no change: don't fight a manual expand
        tick("idle", "hibernated")        # woke
        self.assertEqual(self.term.collapsed, [("g1", True), ("g1", False)])

    def test_restores_group_in_a_new_tab_and_respects_removal(self):
        self.e.state["groups"] = {"s1": {"group": "firequest", "pane": "fake:old", "at": 0},
                                  "s2": {"group": "router", "pane": "fake:2", "at": 0},
                                  "s3": {"group": "", "pane": "fake:3", "at": 0}}
        panes = {k: self.pane(k) for k in ("fake:1", "fake:2", "fake:3")}
        rows = [{"id": f"fake:{i}", "state": "hibernated", "title": "CSTFP-9", "session_id": f"s{i}"} for i in (1, 2, 3)]
        asyncio.run(self.e.sync_groups(panes, rows))
        # s1: same conversation in a new tab (after a reboot) goes back; s2: you took it out of router in
        # this very tab, so it stays out; s3: already out, and the oncall rule doesn't pull it back in.
        self.assertEqual(self.term.filed, [("firequest", ["fake:1"])])
        self.assertEqual(self.e.state["groups"]["s1"]["pane"], "fake:1")
        self.assertEqual(self.e.state["groups"]["s2"]["group"], "")
        self.assertEqual(self.e.group_for(panes["fake:3"], "CSTFP-9", "/", "s3"), "")

    def test_just_filed_tab_is_not_mistaken_for_removed(self):
        panes = {"fake:1": self.pane("fake:1")}
        rows = [{"id": "fake:1", "state": "idle", "title": "CSTFP-1", "session_id": "s1"}]
        asyncio.run(self.e.sync_groups(panes, rows))
        asyncio.run(self.e.sync_groups(panes, rows))  # iTerm2 hasn't reported the group yet
        self.assertEqual(self.e.state["groups"]["s1"]["group"], "oncall")

    def test_asks_stale_keep_and_close(self):
        self.e.cfg.stale_days = 7
        self.e.panes = {"fake:1": self.pane("fake:1")}
        self.e.state["tabs"]["fake:1"] = {"session_id": "s1", "title": "old", "command": "claude --resume s1",
                                          "agent": "claude", "cwd": "/"}
        rows = [{"id": "fake:1", "state": "hibernated", "title": "old", "session_id": "s1", "asleep_min": 8 * 1440},
                {"id": "fake:2", "state": "hibernated", "title": "new", "session_id": "s2", "asleep_min": 60}]
        self.e.asks = self.e.build_asks(rows)
        self.assertEqual([a["id"] for a in self.e.asks], ["stale:s1"])
        asyncio.run(self.e.answer("stale:s1", "keep"))
        self.assertEqual(self.e.build_asks(rows), [])
        self.e.state["keep"] = {}
        self.e.asks = self.e.build_asks(rows)
        asyncio.run(self.e.answer("stale:s1", "close"))
        self.assertEqual(self.term.closed, ["fake:1"])
        self.assertNotIn("fake:1", self.e.state["tabs"])
        self.assertEqual(self.e.state["closed"][-1]["command"], "claude --resume s1")

    def test_group_suggestion_only_files_on_yes(self):
        self.e.panes = {"fake:1": self.pane("fake:1")}
        self.e.rows = [{"id": "fake:1", "session_id": "s1"}]
        self.e.state["suggested"] = {"s1": {"group": "firequest", "answer": None}}
        rows = [{"id": "fake:1", "state": "idle", "title": "pipeline", "session_id": "s1", "group": ""}]
        self.e.asks = self.e.build_asks(rows)
        self.assertEqual(self.term.filed, [])
        asyncio.run(self.e.answer("group:s1", "no"))
        self.assertEqual((self.term.filed, self.e.build_asks(rows)), ([], []))
        self.e.state["suggested"]["s1"]["answer"] = None
        self.e.asks = self.e.build_asks(rows)
        asyncio.run(self.e.answer("group:s1", "yes"))
        self.assertEqual(self.term.filed, [("firequest", ["fake:1"])])
        self.assertEqual(self.e.state["groups"]["s1"]["group"], "firequest")

    def test_assign_groups_one_call_per_group_and_validates(self):
        self.e.panes = {k: self.pane(k, group="old" if k == "fake:2" else "") for k in ("fake:1", "fake:2")}
        asyncio.run(self.e.assign_groups({"firequest": ["fake:1", "fake:2"]}))
        self.assertEqual(self.term.filed, [("firequest", ["fake:1", "fake:2"])])
        self.assertEqual(self.e.grouped, {"fake:1", "fake:2"})
        with self.assertRaises(ActionError):
            asyncio.run(self.e.assign_groups({"x": ["fake:404"]}))
        with self.assertRaises(ActionError):
            asyncio.run(self.e.assign_groups(["fake:1"]))

    def test_tab_without_agent_keeps_group_open(self):
        panes = {"fake:1": self.pane("fake:1", "oncall", "g1"), "fake:9": self.pane("fake:9", "oncall", "g1")}
        asyncio.run(self.e.sync_groups(panes, [{"id": "fake:1", "state": "hibernated", "group": "oncall"}]))
        self.assertEqual(self.term.collapsed, [])

if __name__ == "__main__":
    unittest.main()
