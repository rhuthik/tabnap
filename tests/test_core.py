import struct
import unittest

from tabnap import procs
from tabnap.agents import top_level
from tabnap.agents.base import kept_flags
from tabnap.engine import auto_decision, fmt_minutes, strip_glyph
from tabnap.terminals.tmux import parse_panes, strip_faint


def facts(**over):
    f = {"pinned": False, "status": "idle", "transcript": "/t.jsonl", "confident": True, "background": False,
         "loop_tool": None, "focused": False, "last_active": 1000.0}
    f.update(over)
    return f


class StripGlyph(unittest.TestCase):
    def test_strips_agent_glyphs_and_job_suffix(self):
        self.assertEqual(strip_glyph("✳ Fix the parser"), "Fix the parser")
        self.assertEqual(strip_glyph("⠹ Fix the parser (claude)"), "Fix the parser")
        self.assertEqual(strip_glyph("Fix the parser (codex)"), "Fix the parser")

    def test_keeps_titles_without_glyph(self):
        self.assertEqual(strip_glyph("bajaj monitoring"), "bajaj monitoring")
        self.assertEqual(strip_glyph("(draft) notes"), "(draft) notes")
        self.assertEqual(strip_glyph(None), "")


class Format(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(fmt_minutes(42), "42m")
        self.assertEqual(fmt_minutes(125), "2h05m")
        self.assertEqual(fmt_minutes(120), "2h")
        self.assertEqual(fmt_minutes(3 * 1440), "3d")


class AutoDecision(unittest.TestCase):
    def test_due_after_idle_minutes(self):
        blocker, left = auto_decision(facts(last_active=0), now=41 * 60, idle_minutes=40)
        self.assertIsNone(blocker)
        self.assertLessEqual(left, 0)

    def test_countdown_before_due(self):
        blocker, left = auto_decision(facts(last_active=0), now=10 * 60, idle_minutes=40)
        self.assertIsNone(blocker)
        self.assertAlmostEqual(left, 30)

    def test_blockers(self):
        for over, why in [({"pinned": True}, "kept awake"), ({"status": "working"}, "working"),
                          ({"status": "waiting"}, "waiting"), ({"transcript": None}, "nothing to resume yet"),
                          ({"confident": False}, "session match is a guess"),
                          ({"background": True}, "background job running"),
                          ({"loop_tool": "ScheduleWakeup"}, "uses ScheduleWakeup"),
                          ({"focused": True}, "tab in focus")]:
            self.assertEqual(auto_decision(facts(last_active=0, **over), 99999, 40)[0], why)


class Processes(unittest.TestCase):
    CLAUDE = "/Users/x/.nvm/versions/node/v20.20.0/bin/claude --resume abc"
    TABLE = procs.parse_ps("\n".join([
        f"100 1 ttys001 {CLAUDE}",
        "101 100 ttys001 node /bin/codex-mcp-server",
        "102 100 ttys001 /bin/claude --print subagent",
        "200 1 ttys002 -zsh",
        f"300 1 ttys003 {CLAUDE}",
        "301 300 ttys003 /bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh && npm run dev",
        "302 301 ttys003 node vite",
        "400 1 ?? /bin/claude.exe --chrome-native-host",
        "500 499 ttys004 node /Users/x/.nvm/versions/node/v20/bin/codex -m gpt-x",
        "501 500 ttys004 /Users/x/.nvm/lib/node_modules/@openai/codex/vendor/bin/codex -m gpt-x",
        "502 1 ?? /Users/x/.codex/packages/bin/codex app-server --listen unix://",
        "600 1 ttys005 codex exec do something",
        "700 1 ttys006 claude -p summarize",
        "800 799 ttys007 /bin/claude",
        "801 800 ttys007 node /usr/bin/codex",  # Codex launched from inside Claude: Claude owns the tab
    ]))

    def test_top_level_agent_per_tty(self):
        found = {tty: (pid, a.name) for tty, (pid, a) in top_level(self.TABLE).items()}
        self.assertEqual(found, {"/dev/ttys001": (100, "claude"), "/dev/ttys003": (300, "claude"),
                                 "/dev/ttys004": (500, "codex"), "/dev/ttys007": (800, "claude")})

    def test_descendants(self):
        kids = procs.children_map(self.TABLE)
        self.assertEqual(sorted(procs.descendants(kids, 300)), [301, 302])

    def test_parse_top_footprint(self):
        out = "PID    MEM\n34372  301M+\n28171  1.2G\n9  512K-\nProcesses: 1"
        self.assertEqual(procs.parse_top(out), {34372: 301.0, 28171: 1.2 * 1024, 9: 0.5})

    def test_procargs2_keeps_spaces_and_env(self):
        data = struct.pack("i", 3) + b"/bin/claude\0\0\0claude\0--append-system-prompt\0be brief please\0" \
            b"CLAUDE_CONFIG_DIR=/x/y\0TERM=xterm\0\0junk"
        argv, env = procs.parse_procargs2(data)
        self.assertEqual(argv, ["claude", "--append-system-prompt", "be brief please"])
        self.assertEqual(env, {"CLAUDE_CONFIG_DIR": "/x/y", "TERM": "xterm"})


class KeptFlags(unittest.TestCase):
    SPEC = {"--model": 1, "--yolo": 0, "--add-dir": "*", "-c": 1}

    def test_keeps_listed_flags_with_values(self):
        argv = ["--model", "haiku", "fix it", "--yolo", "--add-dir", "a", "b c", "--verbose", "-c", "k=v", "--model=x"]
        self.assertEqual(kept_flags(argv, self.SPEC), "--model haiku --yolo --add-dir a 'b c' -c k=v --model=x")

    def test_nothing_to_keep(self):
        self.assertEqual(kept_flags(["--resume", "abc"], self.SPEC), "")


class Tmux(unittest.TestCase):
    def test_parse_panes(self):
        line = "\t".join(["%3", "/dev/ttys009", "zsh", "1", "1", "1", "@2", "main", "2", "0", "✳ Fix"])
        rows = parse_panes(line + "\nbroken line")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pane_id"], "%3")
        self.assertEqual(rows[0]["pane_title"], "✳ Fix")

    def test_strip_faint(self):
        self.assertEqual(strip_faint("\x1b[39m❯\xa0\x1b[2mcount from 9 to 12\x1b[0m"), "❯\xa0")
        self.assertEqual(strip_faint("\x1b[39m❯\xa0abc"), "❯\xa0abc")
        # 38;5;2 is a color index, not "dim"
        self.assertEqual(strip_faint("\x1b[38;5;2mgreen\x1b[0m \x1b[2;3mdim\x1b[22m back"), "green  back")


if __name__ == "__main__":
    unittest.main()
