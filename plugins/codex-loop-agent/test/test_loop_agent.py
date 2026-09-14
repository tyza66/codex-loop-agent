import datetime as dt
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "loop-agent.py"
)
SPEC = importlib.util.spec_from_file_location("loop_agent", SCRIPT)
loop_agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loop_agent)

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER_SID = "11111111-2222-3333-4444-555555555555"


def session_file(sessions_root: Path, session_id: str, cwd: str, *, mtime=None) -> Path:
    path = (
        sessions_root
        / "2026"
        / "09"
        / "13"
        / f"rollout-2026-09-13T00-00-00-{session_id}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "session_meta",
            "timestamp": "2026-09-13T00:00:00.000Z",
            "payload": json.dumps({"session_id": session_id}),
        },
        {
            "type": "turn_context",
            "timestamp": "2026-09-13T00:00:01.000Z",
            "payload": json.dumps({"type": "turn_context", "cwd": cwd}),
        },
        {
            "type": "response_item",
            "timestamp": "2026-09-13T00:00:02.000Z",
            "payload": json.dumps(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer text"}],
                }
            ),
        },
        {
            "type": "event_msg",
            "timestamp": "2026-09-13T00:00:03.000Z",
            "payload": json.dumps({"type": "user_message", "message": "original task"}),
        },
        {
            "type": "event_msg",
            "timestamp": "2026-09-13T00:00:04.000Z",
            "payload": json.dumps({"type": "task_complete"}),
        },
    ]
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class LoopAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "codex"
        self.sessions_root = self.home / "sessions"
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        previous = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = str(self.home)
        self.addCleanup(lambda: set_codex_home(previous))
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        previous_path = os.environ.get("PATH")
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + (previous_path or "")
        self.addCleanup(lambda: restore_env("PATH", previous_path))

    def test_render_continuation(self):
        rendered = loop_agent.render_continuation(
            "上一轮说：{{lastAnswer}}；第 {{round}} 轮；任务 {{task}}",
            "修复完成",
            3,
            "测试任务",
        )
        self.assertIn("上一轮说：修复完成", rendered)
        self.assertIn("第 3 轮", rendered)
        self.assertIn("任务 测试任务", rendered)

    def test_is_stop_message(self):
        for text in (
            "/stop",
            "/forever stop",
            "停止无尽模式",
            "停止无限循环",
            "退出无尽模式",
            "停止循环",
            "停一下",
            "stop the loop",
        ):
            self.assertTrue(loop_agent.is_stop_message(text), text)
        for text in ("继续检查", "不要停止", "请继续"):
            self.assertFalse(loop_agent.is_stop_message(text), text)

    def test_event_parsing_and_assistant_text(self):
        path = session_file(self.sessions_root, SID, "/tmp/project")
        events = loop_agent.read_events(path)
        self.assertEqual(loop_agent.last_assistant_text(events), "answer text")
        self.assertEqual(len(loop_agent.event_user_messages(events)), 1)
        self.assertEqual(loop_agent.event_user_messages(events)[0]["text"], "original task")
        self.assertGreater(loop_agent.last_completion_ts(events), 0)

    def test_resolve_session_by_cwd_prefers_newest_match(self):
        old_path = session_file(self.sessions_root, SID, "/tmp/project", mtime=1000)
        new_path = session_file(self.sessions_root, OTHER_SID, "/tmp/project", mtime=2000)
        session_id, path = loop_agent.resolve_session(None, "/tmp/project", False)
        self.assertEqual(session_id, OTHER_SID)
        self.assertEqual(path, new_path)
        session_id, path = loop_agent.resolve_session(SID, None, False)
        self.assertEqual(session_id, SID)
        self.assertEqual(path, old_path)

    def test_explicit_session_wins(self):
        session_file(self.sessions_root, SID, "/tmp/old", mtime=1000)
        session_file(self.sessions_root, OTHER_SID, "/tmp/new", mtime=2000)
        session_id, _ = loop_agent.resolve_session(SID, "/tmp/new", False)
        self.assertEqual(session_id, SID)

    def test_pending_human_message(self):
        messages = [
            {"ts": 10.0, "text": "loop prompt", "sha": loop_agent.sha256_text("loop prompt")},
            {"ts": 20.0, "text": "user: wait", "sha": loop_agent.sha256_text("user: wait")},
        ]
        sent = {loop_agent.sha256_text("loop prompt")}
        pending = loop_agent.pending_human_message(messages, sent, 15.0)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["text"], "user: wait")
        self.assertIsNone(loop_agent.pending_human_message(messages, sent, 20.0))

    def test_global_config_round_trip(self):
        config = loop_agent.load_global_config()
        self.assertFalse(config["disabled"])
        config["continuation"] = "继续"
        path = loop_agent.save_global_config(config)
        self.assertTrue(path.exists())
        self.assertEqual(loop_agent.load_global_config()["continuation"], "继续")

    def test_run_loop_completes_one_round_with_fake_codex(self):
        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "assert os.getcwd() == os.environ['FAKE_CODEX_CWD']\n"
            "args = sys.argv[1:]\n"
            "prompt = args[3]\n"
            "path = os.environ['FAKE_SESSION_FILE']\n"
            "ts = '2026-09-13T00:00:10.000Z'\n"
            "events = [\n"
            "    {'type': 'event_msg', 'timestamp': ts, 'payload': json.dumps({'type': 'user_message', 'message': prompt})},\n"
            "    {'type': 'response_item', 'timestamp': ts, 'payload': json.dumps({'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'loop answer'}]})},\n"
            "    {'type': 'event_msg', 'timestamp': ts, 'payload': json.dumps({'type': 'task_complete'})},\n"
            "]\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    for event in events:\n"
            "        handle.write(json.dumps(event) + '\\n')\n"
            "sys.exit(0)\n"
        )
        fake_codex.chmod(0o755)
        session = session_file(self.sessions_root, SID, str(self.home))
        os.environ["FAKE_SESSION_FILE"] = str(session)
        os.environ["FAKE_CODEX_CWD"] = str(self.home.resolve())
        state = loop_agent.load_state(SID)
        state.update(
            {
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "max_rounds": 1,
                "poll_ms": 1,
                "quiet": True,
            }
        )
        rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        final = loop_agent.load_state(SID)
        self.assertEqual(final["rounds"], 1)
        self.assertEqual(final["status"], "completed")
        events = loop_agent.read_events(session)
        texts = [m["text"] for m in loop_agent.event_user_messages(events)]
        self.assertEqual(texts, ["original task", "继续"])

    def test_build_codex_command_uses_resume_compatible_flags(self):
        command = loop_agent.build_codex_command(SID, "继续任务")
        self.assertEqual(
            command[1:],
            [
                "exec",
                "resume",
                SID,
                "继续任务",
                "--skip-git-repo-check",
                "--json",
            ],
        )
        for unsupported_flag in ("--cd", "--color", "--approve-for-me"):
            self.assertNotIn(unsupported_flag, command)


def set_codex_home(value):
    if value is None:
        os.environ.pop("CODEX_HOME", None)
    else:
        os.environ["CODEX_HOME"] = value


def restore_env(key, value):
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
