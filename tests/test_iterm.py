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


if __name__ == "__main__":
    unittest.main()
