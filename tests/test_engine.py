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
    state_dir = tempfile.mkdtemp()
    with mock.patch("tabnap.engine.STATE_FILE", os.path.join(state_dir, "state.json")):
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
