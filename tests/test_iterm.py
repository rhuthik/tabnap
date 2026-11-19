import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import iterm2

from tabnap.config import Config
from tabnap.terminals import iterm as iterm_mod
from tabnap.terminals.iterm import ITerm


class FakeSession:
    def __init__(self, sid, gone=False):
        self.session_id, self.gone = sid, gone

    async def async_get_variable(self, name):
        if self.gone:
            raise iterm2.RPCException("SESSION_NOT_FOUND")
        return {"tty": f"/dev/tty-{self.session_id}", "autoName": self.session_id}[name]


def fake_app(sessions):
    tab = SimpleNamespace(sessions=sessions)
    window = SimpleNamespace(window_id="w1", tabs=[tab], current_tab=None)

    async def refresh():
        pass
    return SimpleNamespace(terminal_windows=[window], current_terminal_window=window, async_refresh_focus=refresh)


class ITermTest(unittest.TestCase):
    def test_tab_closed_mid_scan_is_skipped_not_lost(self):
        t = ITerm(Config())
        t.app = fake_app([FakeSession("a"), FakeSession("b", gone=True)])
        panes = asyncio.run(t.panes())
        self.assertEqual([p.ref for p in panes], ["a"])

    def test_reconnect_closes_the_old_connection(self):
        # The old connection keeps its RPC registration while open, so a new one fails with
        # DUPLICATE_SERVER_ORIGINATED_RPC until the old socket is closed.
        t = ITerm(Config())
        old_ws = mock.AsyncMock()
        t.conn = SimpleNamespace(websocket=old_ws)
        boom = RuntimeError("stop after close")
        with mock.patch.object(iterm_mod, "iterm_running", return_value=True), \
                mock.patch.object(iterm2.Connection, "async_create", side_effect=boom):
            with self.assertRaises(RuntimeError):
                asyncio.run(t.start(SimpleNamespace()))
        old_ws.close.assert_awaited_once()


    def test_set_group_creates_once_per_window_then_joins(self):
        cfg = Config()
        cfg.iterm_group_colors = {"oncall": "#5c3a3a"}
        t = ITerm(cfg)
        s1, s2, s3 = FakeSession("a"), FakeSession("b"), FakeSession("c")
        tabs = [SimpleNamespace(tab_id=str(i), joined=[]) for i in range(3)]
        for tab in tabs:
            async def add(group, tab=tab):
                tab.joined.append(group)
            tab.async_add_to_tab_group = add
        window = SimpleNamespace(window_id="w1", tab_groups=[], created=[])

        async def create(name, tabs_, color=None):
            window.created.append((name, [x.tab_id for x in tabs_], color.hex))
            window.tab_groups.append(SimpleNamespace(name=name, group_id="g1"))
            return window.tab_groups[-1]
        window.async_create_tab_group = create
        where = {"a": tabs[0], "b": tabs[0], "c": tabs[1]}  # a and b are split panes of one tab
        t.app = SimpleNamespace(get_session_by_id=lambda sid: {"a": s1, "b": s2, "c": s3}[sid],
                                get_window_and_tab_for_session=lambda s: (window, where[s.session_id]))
        t.conn = object()
        pane = lambda sid: SimpleNamespace(ref=sid)
        with mock.patch.object(iterm_mod, "supports_tab_groups", return_value=True):
            asyncio.run(t.set_group([pane("a"), pane("b")], "oncall"))
            asyncio.run(t.set_group([pane("c")], "oncall"))
        self.assertEqual(window.created, [("oncall", ["0"], "#5c3a3a")])
        self.assertEqual(t.colored, {"g1"})
        self.assertEqual([g.group_id for g in tabs[1].joined], ["g1"])


if __name__ == "__main__":
    unittest.main()
