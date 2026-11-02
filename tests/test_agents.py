import json
import os
import sqlite3
import tempfile
import unittest

from tabnap.agents.claude import Claude, last_context, recent_loop_tool, transcript_title
from tabnap.agents.codex import Codex, pick_thread, rollout_context, rollout_status
from tabnap.config import HOME, Config

RULE = "─" * 40


def tmpfile(text, suffix=".jsonl"):
    fh = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False)
    fh.write(text)
    fh.close()
    return fh.name


class ClaudeScreen(unittest.TestCase):
    c = Claude()

    def test_empty_input_box_is_fine(self):
        self.assertIsNone(self.c.input_blocker(["⏺ ok", "", RULE, "❯ ", RULE, "  Opus 5.5"]))
        self.assertIsNone(self.c.input_blocker(["⏺ ok", RULE, "❯\xa0", RULE]))  # suggestion text removed

    def test_draft_blocks(self):
        self.assertEqual(self.c.input_blocker(["⏺ ok", RULE, "❯ half typed thought", RULE]), "draft in input box")

    def test_dialog_without_input_box_blocks(self):
        self.assertEqual(self.c.input_blocker(["Do you want to proceed?", "❯ 1. Yes", "  2. No"]),
                         "dialog or menu open")

    def test_earlier_prompt_echo_is_not_the_input_box(self):
        self.assertIsNone(self.c.input_blocker(["❯ reply with ok", "", "⏺ ok", RULE, "❯ ", RULE]))

    def test_exit_line_and_id(self):
        lines = ["⏺ bye", "Resume this session with:", "claude --resume 34d29871-d975-4de3-888b-dea7b8958423", "$ "]
        self.assertEqual(self.c.exit_line(lines), 1)
        self.assertEqual(self.c.exit_session_id(lines), "34d29871-d975-4de3-888b-dea7b8958423")

    def test_matches(self):
        self.assertTrue(self.c.matches(["/usr/local/bin/claude", "--model", "haiku"]))
        self.assertFalse(self.c.matches(["claude", "-p", "hi"]))
        self.assertFalse(self.c.matches(["claude", "mcp", "serve"]))
        self.assertFalse(self.c.matches(["claude-mcp-server"]))


class ClaudeResume(unittest.TestCase):
    c = Claude()

    def test_default_dir(self):
        cmd = self.c.resume_command(Config(), "abc", ["claude", "--model", "haiku", "fix it"],
                                    {"config_dir": os.path.join(HOME, ".claude")})
        self.assertEqual(cmd, "claude --resume abc --model haiku")

    def test_other_dir_gets_env_prefix(self):
        cmd = self.c.resume_command(Config(), "abc", ["claude"], {"config_dir": "/p/work"})
        self.assertEqual(cmd, "CLAUDE_CONFIG_DIR=/p/work claude --resume abc")

    def test_per_dir_template(self):
        cfg = Config(claude_resume_by_dir={"/p/*": "claude-{profile} --resume {id}"})
        cmd = self.c.resume_command(cfg, "abc", ["claude", "--dangerously-skip-permissions"], {"config_dir": "/p/work"})
        self.assertEqual(cmd, "claude-work --resume abc --dangerously-skip-permissions")
        cfg = Config(claude_resume_by_dir={"/p/*": "claude --resume {id}"})  # a wrapper/alias sets the dir
        self.assertEqual(self.c.resume_command(cfg, "abc", ["claude"], {"config_dir": "/p/work"}), "claude --resume abc")


class ClaudeTranscript(unittest.TestCase):
    def test_loop_tool_in_tail_only(self):
        path = tmpfile('{"type":"tool_use","id":"t1","name":"ScheduleWakeup"}\n' + "x" * 300_000 + "\n"
                       '{"type":"tool_use","id":"t2","name":"Bash"}\n')
        try:
            self.assertIsNone(recent_loop_tool(path))
            with open(path, "a") as fh:
                fh.write('{"type":"tool_use","id":"t3","name":"CronCreate"}\n')
            self.assertEqual(recent_loop_tool(path), "CronCreate")
        finally:
            os.remove(path)

    def test_tool_definitions_are_not_calls(self):
        path = tmpfile('{"tools":[{"name":"ScheduleWakeup","description":"Schedule when to resume"}]}\n')
        try:
            self.assertIsNone(recent_loop_tool(path))
        finally:
            os.remove(path)

    def test_rename_wins_over_generated_title(self):
        path = tmpfile('{"type":"ai-title","aiTitle":"Fix the parser"}\n'
                       '{"type":"custom-title","customTitle":"Parser \\"v2\\" work"}\n'
                       '{"type":"ai-title","aiTitle":"Later generated title"}\n')
        try:
            self.assertEqual(transcript_title(path), 'Parser "v2" work')
            self.assertIsNone(transcript_title("/nonexistent.jsonl"))
        finally:
            os.remove(path)

    def test_last_context_skips_meta_tool_results_and_commands(self):
        entries = [
            {"type": "user", "message": {"content": "fix the   parser\nplease"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed it."}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "Base directory for this skill"}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
        ]
        path = tmpfile("half a line from before the tail}\n" + "\n".join(json.dumps(e) for e in entries) + "\n")
        try:
            self.assertEqual(last_context(path), {"answer": "Fixed it.", "prompt": "fix the parser please"})
            self.assertEqual(last_context("/nonexistent.jsonl"), {})
        finally:
            os.remove(path)


def rollout(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def ev(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def msg(role, text):
    kind = "output_text" if role == "assistant" else "input_text"
    return {"type": "response_item", "payload": {"type": "message", "role": role,
                                                 "content": [{"type": kind, "text": text}]}}


class CodexRollout(unittest.TestCase):
    def test_status(self):
        cases = [((ev("task_started"),), "working"),
                 ((ev("task_started"), ev("task_complete")), "idle"),
                 ((ev("task_started"), ev("turn_aborted")), "idle"),
                 ((ev("task_started"), ev("exec_approval_request")), "waiting"),
                 ((), "idle")]
        for events, want in cases:
            path = tmpfile(rollout(*events) if events else "")
            try:
                self.assertEqual(rollout_status(path), want, events)
            finally:
                os.remove(path)

    def test_context_skips_injected_blocks(self):
        path = tmpfile(rollout(msg("user", "<environment_context>\n<cwd>/x</cwd>"), msg("user", "fix the  bug"),
                               msg("assistant", "Done."), msg("user", "<user_instructions>be nice")))
        try:
            self.assertEqual(rollout_context(path), {"answer": "Done.", "prompt": "fix the bug"})
        finally:
            os.remove(path)


class CodexThreads(unittest.TestCase):
    def test_pick_thread(self):
        rows = [("old", "/r1", "t", 1000, 2000), ("new", "/r2", "t", 50_000, 60_000), ("resumed", "/r3", "t", 10, 55_000)]
        self.assertEqual(pick_thread(rows, 49_000, set()), (rows[1], True))
        self.assertEqual(pick_thread(rows, 49_000, {"new"}), (rows[2], True))
        self.assertEqual(pick_thread(rows, 99_000, set()), (None, False))

    def test_threads_from_state_db(self):
        home = tempfile.mkdtemp()
        con = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
        con.execute("create table threads (id text, rollout_path text, title text, name text, cwd text, source text, "
                    "archived int, agent_role text, created_at_ms int, updated_at_ms int)")
        con.executemany("insert into threads values (?,?,?,?,?,?,?,?,?,?)", [
            ("a", "/ra", "first prompt", "Nice title", "/w", "cli", 0, None, 1, 2),
            ("b", "/rb", "sub", None, "/w", "cli", 0, "explorer", 1, 2),
            ("c", "/rc", "exec", None, "/w", "exec", 0, None, 1, 2),
            ("d", "/rd", "other dir", None, "/elsewhere", "cli", 0, None, 1, 2),
        ])
        con.commit()
        con.close()
        rows = Codex().threads(home, cwd="/w")
        self.assertEqual(rows, [("a", "/ra", "Nice title", 1, 2)])
        self.assertEqual(Codex().threads(home, thread_id="d")[0][0], "d")
        self.assertIsNone(Codex().threads(tempfile.mkdtemp(), cwd="/w"))


class CodexScreen(unittest.TestCase):
    c = Codex()

    def test_input_blocker(self):
        self.assertIsNone(self.c.input_blocker(["• ok", "› Ask Codex to do anything", "  5h 100% left", "  ? for shortcuts"]))
        self.assertIsNone(self.c.input_blocker(["• ok", "› ", "  ? for shortcuts"]))
        self.assertEqual(self.c.input_blocker(["› fix the parser", "  ? for shortcuts"]), "draft in input box")
        self.assertEqual(self.c.input_blocker(["Trust this folder?", "  1. Trust", "  2. Quit"]), "dialog or menu open")

    def test_exit_and_resume(self):
        lines = ["Token usage: total=4,663", "To continue this session, run codex resume 01a19bbb-0139-77e1-b05c-b31e6d54c9d6"]
        self.assertEqual(self.c.exit_session_id(lines), "01a19bbb-0139-77e1-b05c-b31e6d54c9d6")
        self.assertEqual(self.c.exit_line(lines), 1)
        cmd = self.c.resume_command(Config(), "abc", ["codex", "-m", "gpt-x", "--yolo", "-C", "/x", "do it"])
        self.assertEqual(cmd, "codex resume abc -m gpt-x --yolo")

    def test_matches(self):
        self.assertTrue(self.c.matches(["node", "/u/bin/codex", "-m", "x"]))
        self.assertTrue(self.c.matches(["codex", "resume", "abc"]))
        self.assertTrue(self.c.matches(["codex"]))
        self.assertFalse(self.c.matches(["codex", "exec", "hi"]))
        self.assertFalse(self.c.matches(["/x/codex", "app-server", "--listen"]))
        self.assertFalse(self.c.matches(["node", "/u/bin/codex-mcp-server"]))


if __name__ == "__main__":
    unittest.main()
