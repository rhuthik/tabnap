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


class ProcessIdentity(unittest.TestCase):
    def test_identity_is_stable_and_specific(self):
        me = procs.proc_start(os.getpid())
        self.assertTrue(me)
        self.assertEqual(me, procs.proc_start(os.getpid()))
        self.assertEqual(procs.proc_start(999999), "")


class Service(unittest.TestCase):
    def test_systemd_quote(self):
        self.assertEqual(systemd_quote('/opt/my apps/py "x" 100%$HOME'), '"/opt/my apps/py \\"x\\" 100%%$$HOME"')


if __name__ == "__main__":
    unittest.main()
