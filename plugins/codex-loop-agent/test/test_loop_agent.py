import datetime as dt
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_original_task_uses_first_user_message(self):
        messages = [
            {"ts": 1.0, "text": "first request", "sha": "a"},
            {"ts": 2.0, "text": "later request", "sha": "b"},
        ]
        self.assertEqual(loop_agent.original_task_text(messages), "first request")
        self.assertEqual(
            loop_agent.original_task_text(
                [
                    {"ts": 1.0, "text": "/forever", "sha": "a"},
                    {"ts": 2.0, "text": "/stop", "sha": "b"},
                    {"ts": 3.0, "text": "real task", "sha": "c"},
                ]
            ),
            "real task",
        )

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

    def test_event_user_messages_reads_queue_response_items(self):
        events = [
            {
                "type": "response_item",
                "timestamp": "2026-09-13T00:00:01.000Z",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "queued continuation",
                        }
                    ],
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-13T00:00:02.000Z",
                "payload": {
                    "type": "user_message",
                    "message": "classic user message",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-09-13T00:00:03.000Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                },
            },
        ]
        messages = loop_agent.event_user_messages(events)
        self.assertEqual(
            [m["text"] for m in messages],
            ["queued continuation", "classic user message"],
        )

    def test_current_object_payload_event_schema(self):
        path = self.sessions_root / "2026" / "09" / "13" / "rollout-object.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        events = [
            {
                "timestamp": "2026-09-13T00:00:01.000Z",
                "type": "turn_context",
                "payload": {"cwd": "/tmp/current-schema"},
            },
            {
                "timestamp": "2026-09-13T00:00:02.000Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "object user"},
            },
            {
                "timestamp": "2026-09-13T00:00:03.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
            {
                "timestamp": "2026-09-13T00:00:04.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "object answer"}],
                },
            },
        ]
        with path.open("w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        self.assertEqual(loop_agent.session_cwd(path), "/tmp/current-schema")
        loaded = loop_agent.read_events(path)
        users = loop_agent.event_user_messages(loaded)
        self.assertEqual([m["text"] for m in users], ["object user"])
        self.assertGreater(loop_agent.last_completion_ts(loaded), 0)
        self.assertEqual(loop_agent.last_assistant_text(loaded), "object answer")

    def test_terminal_event_timestamps_separate_abort_from_complete(self):
        events = [
            {
                "timestamp": "2026-09-13T00:00:01.000Z",
                "type": "event_msg",
                "payload": {"type": "task_complete"},
            },
            {
                "timestamp": "2026-09-13T00:00:02.000Z",
                "type": "event_msg",
                "payload": {"type": "turn_aborted", "reason": "interrupted"},
            },
        ]
        completion = loop_agent.last_completion_ts(events)
        aborted = loop_agent.last_aborted_ts(events)
        self.assertGreater(completion, 0)
        self.assertGreater(aborted, completion)

    def test_resolve_session_by_cwd_prefers_newest_match(self):
        old_path = session_file(self.sessions_root, SID, "/tmp/project", mtime=1000)
        new_path = session_file(self.sessions_root, OTHER_SID, "/tmp/project", mtime=2000)
        session_id, path = loop_agent.resolve_session(None, "/tmp/project", False)
        self.assertEqual(session_id, OTHER_SID)
        self.assertEqual(path, new_path)
        session_id, path = loop_agent.resolve_session(SID, None, False)
        self.assertEqual(session_id, SID)
        self.assertEqual(path, old_path)


    def test_last_without_cwd_uses_newest_session(self):
        session_file(self.sessions_root, SID, "/tmp/project", mtime=1000)
        newest = session_file(self.sessions_root, OTHER_SID, "/other", mtime=2000)
        session_id, path = loop_agent.resolve_session(None, None, True)
        self.assertEqual(session_id, OTHER_SID)
        self.assertEqual(path, newest)

    def test_last_with_cwd_keeps_cwd_scope(self):
        newest = session_file(self.sessions_root, SID, "/tmp/project", mtime=1000)
        session_file(self.sessions_root, OTHER_SID, "/other", mtime=2000)
        session_id, path = loop_agent.resolve_session(None, "/tmp/project", True)
        self.assertEqual(session_id, SID)
        self.assertEqual(path, newest)

    def test_last_with_unknown_cwd_fails(self):
        session_file(self.sessions_root, SID, "/tmp/project", mtime=1000)
        with self.assertRaises(RuntimeError):
            loop_agent.resolve_session(None, "/tmp/missing", True)

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

    def test_run_loop_detects_stop_even_when_not_latest_message(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        with session.open("a", encoding="utf-8") as handle:
            for ts, msg in (
                ("2026-09-13T00:00:10.000Z", "/stop"),
                ("2026-09-13T00:00:11.000Z", "继续"),
            ):
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "timestamp": ts,
                            "payload": {"type": "user_message", "message": msg},
                        }
                    )
                    + "\n"
                )
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=AssertionError("must not resume")
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        final = loop_agent.load_state(SID)
        self.assertEqual(final["status"], "stopped")
        self.assertTrue(final["stop_requested"])

    def test_global_config_round_trip(self):
        config = loop_agent.load_global_config()
        self.assertFalse(config["disabled"])
        config["continuation"] = "继续"
        path = loop_agent.save_global_config(config)
        self.assertTrue(path.exists())
        self.assertEqual(loop_agent.load_global_config()["continuation"], "继续")

    def test_config_rejects_empty_continuation(self):
        with self.assertRaises(SystemExit):
            loop_agent.main(["config", "set", "continuation", "  "])

    def test_config_rejects_unknown_key(self):
        with self.assertRaises(SystemExit):
            loop_agent.main(["config", "set", "bogus", "x"])

    def test_is_same_process_matches_expected_signature(self):
        with mock.patch.object(
            loop_agent, "process_start_signature", return_value="sig-current"
        ):
            self.assertTrue(
                loop_agent.is_same_process(os.getpid(), "sig-current")
            )

    def test_is_same_process_detects_pid_reuse(self):
        with mock.patch.object(
            loop_agent, "process_start_signature", return_value="sig-current"
        ):
            self.assertFalse(
                loop_agent.is_same_process(
                    os.getpid(), "Mon Jan  1 00:00:00 1970"
                )
            )

    def test_is_same_process_without_signature_is_not_a_match(self):
        with mock.patch.object(
            loop_agent, "process_start_signature", return_value=None
        ):
            self.assertFalse(
                loop_agent.is_same_process(os.getpid(), "sig-recorded")
            )

    def test_legacy_state_without_signature_uses_command_check(self):
        with mock.patch.object(
            loop_agent, "process_command_matches", return_value=True
        ):
            self.assertTrue(loop_agent.is_same_process(os.getpid(), None))
        with mock.patch.object(
            loop_agent, "process_command_matches", return_value=False
        ):
            self.assertFalse(loop_agent.is_same_process(os.getpid(), None))

    def test_is_same_process_rejects_dead_pid(self):
        self.assertFalse(loop_agent.is_same_process(os.getpid() + 1000000, None))
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
                "transport": "exec",
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

    def test_run_loop_does_not_buffer_codex_stdout(self):
        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text("#!/usr/bin/env python3\nsys.exit(0)\n")
        fake_codex.chmod(0o755)
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "max_rounds": 1,
                "poll_ms": 1,
                "quiet": True,
            }
        )
        captured = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "", ""

        def capture(*args, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ):
            rc = loop_agent.run_loop(SID, session, state)

        self.assertEqual(rc, 0)
        self.assertEqual(captured["stdout"], loop_agent.subprocess.DEVNULL)

    def test_run_loop_retries_after_generic_resume_error(self):
        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text("#!/usr/bin/env python3\nsys.exit(0)\n")
        fake_codex.chmod(0o755)
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            raise OSError("boom")

        def mark_stop(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent.time, "sleep", side_effect=mark_stop
        ), mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=flaky
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(len(calls), 1)
        self.assertEqual(rc, 0)
        self.assertEqual(loop_agent.load_state(SID)["status"], "stopped")

    def test_run_loop_waits_after_killing_timed_out_codex(self):
        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text("#!/usr/bin/env python3\nsys.exit(0)\n")
        fake_codex.chmod(0o755)
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )

        class FakeProcess:
            killed = False
            waited = False

            def communicate(self, timeout=None):
                raise loop_agent.subprocess.TimeoutExpired(
                    "codex", timeout=timeout
                )

            def poll(self):
                return None

            def kill(self):
                self.killed = True

            def wait(self):
                self.waited = True
                return -9

        fake_process = FakeProcess()

        def mark_stop(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent.time, "sleep", side_effect=mark_stop
        ), mock.patch.object(
            loop_agent.subprocess, "Popen", return_value=fake_process
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertTrue(fake_process.killed)
        self.assertTrue(fake_process.waited)
        self.assertEqual(rc, 0)

    def test_interrupted_turn_is_retried_not_treated_as_stop(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-09-13T00:00:05.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_aborted",
                            "reason": "interrupted",
                        },
                    }
                )
                + "\n"
            )
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )

        class FakeProcess:
            returncode = 1

            def communicate(self, timeout=None):
                return "", "transient failure"

        def mark_stop(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent.time, "sleep", side_effect=mark_stop
        ), mock.patch.object(
            loop_agent.subprocess, "Popen", return_value=FakeProcess()
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        final = loop_agent.load_state(SID)
        self.assertEqual(final["status"], "stopped")

    def test_start_uses_session_cwd_even_when_dir_is_default(self):
        session = session_file(self.sessions_root, SID, "/tmp/session-cwd")
        args = loop_agent.build_parser().parse_args(["start", "--session", SID])
        idle = loop_agent.load_state(SID)
        running = dict(
            idle,
            pid=os.getpid(),
            pid_started="test-signature",
            status="running",
        )
        states = iter([idle])
        captured = {}

        class FakeProcess:
            pid = os.getpid()
            def poll(self):
                return None

        def capture(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            return FakeProcess()

        def next_state(_sid):
            return next(states, running)

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", return_value=(SID, session)
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=next_state
        ), mock.patch.object(
            loop_agent, "save_state", return_value=loop_agent.state_path(SID)
        ):
            rc = loop_agent.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(
            str(Path(captured["cwd"]).resolve()), str(Path("/tmp/session-cwd").resolve())
        )
        self.assertIn("--dir-from-session", captured["command"])

    def test_explicit_dir_wins_over_session_cwd(self):
        session = session_file(self.sessions_root, SID, "/tmp/session-cwd")
        explicit_dir = Path(self.tmp.name) / "explicit"
        explicit_dir.mkdir()
        args = loop_agent.build_parser().parse_args(
            ["start", "--session", SID, "--dir", str(explicit_dir)]
        )
        idle = loop_agent.load_state(SID)
        running = dict(
            idle,
            pid=os.getpid(),
            pid_started="test-signature",
            status="running",
        )
        states = iter([idle])
        captured = {}

        class FakeProcess:
            pid = os.getpid()
            def poll(self):
                return None

        def capture(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            return FakeProcess()

        def next_state(_sid):
            return next(states, running)

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", return_value=(SID, session)
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=next_state
        ), mock.patch.object(
            loop_agent, "save_state", return_value=loop_agent.state_path(SID)
        ):
            rc = loop_agent.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(
            str(Path(captured["cwd"]).resolve()), str(explicit_dir.resolve())
        )

    def test_start_last_without_dir_resolves_globally(self):
        recent = session_file(self.sessions_root, OTHER_SID, "/other", mtime=2000)
        args = loop_agent.build_parser().parse_args(["start", "--last"])
        captured = {}

        class FakeProcess:
            pid = os.getpid()
            def poll(self):
                return None

        def fake_resolve(session, cwd, last):
            captured["resolve"] = (session, cwd, last)
            return OTHER_SID, recent

        def capture(command, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            return FakeProcess()

        idle = loop_agent.load_state(OTHER_SID)
        running = dict(
            idle,
            pid=os.getpid(),
            pid_started="test-signature",
            status="running",
        )
        states = iter([idle])

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", side_effect=fake_resolve
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=lambda _sid: next(states, running)
        ), mock.patch.object(
            loop_agent, "save_state", return_value=loop_agent.state_path(OTHER_SID)
        ):
            rc = loop_agent.cmd_start(args)

        self.assertEqual(rc, 0)
        self.assertEqual(captured["resolve"][1], None)
        self.assertEqual(captured["resolve"][2], True)

    def test_background_child_forwards_unobserved_timeout(self):
        """The watchdog must survive the background re-exec.

        The duplicate-continuation storm happened by default in the
        background child, so the timeout that bounds it has to be passed
        through explicitly (or the child silently falls back to the default).
        """
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        args = loop_agent.build_parser().parse_args(
            [
                "start",
                "--session",
                SID,
                "--dir",
                str(self.home.resolve()),
                "--unobserved-timeout-seconds",
                "12.5",
            ]
        )
        idle = loop_agent.load_state(SID)
        running = dict(
            idle, pid=os.getpid(), pid_started="test-signature", status="running"
        )
        states = iter([idle])
        captured = {}

        class FakeProcess:
            pid = os.getpid()

            def poll(self):
                return None

        def capture(command, **kwargs):
            captured["command"] = command
            return FakeProcess()

        def next_state(_sid):
            return next(states, running)

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", return_value=(SID, session)
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=next_state
        ), mock.patch.object(
            loop_agent, "save_state", return_value=loop_agent.state_path(SID)
        ):
            rc = loop_agent.cmd_start(args)
        self.assertEqual(rc, 0)
        command = captured["command"]
        self.assertIn("--unobserved-timeout-seconds", command)
        flag_index = command.index("--unobserved-timeout-seconds")
        self.assertEqual(command[flag_index + 1], "12.5")
        session = session_file(self.sessions_root, SID, "/tmp/session-cwd")

    def test_background_child_uses_session_cwd_marker(self):
        session = session_file(self.sessions_root, SID, "/tmp/session-cwd")
        args = loop_agent.build_parser().parse_args(
            [
                "start",
                "--session",
                SID,
                "--dir",
                str(self.home.resolve()),
                "--dir-from-session",
            ]
        )
        idle = loop_agent.load_state(SID)
        running = dict(
            idle,
            pid=os.getpid(),
            pid_started="test-signature",
            status="running",
        )
        states = iter([idle])
        captured = {}

        class FakeProcess:
            pid = os.getpid()
            def poll(self):
                return None

        def capture(command, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            return FakeProcess()

        def next_state(_sid):
            return next(states, running)

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", return_value=(SID, session)
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=next_state
        ), mock.patch.object(
            loop_agent, "save_state", return_value=loop_agent.state_path(SID)
        ):
            rc = loop_agent.cmd_start(args)
        self.assertEqual(rc, 0)
        self.assertEqual(
            str(Path(captured["cwd"]).resolve()), str(Path("/tmp/session-cwd").resolve())
        )


    def test_background_start_does_not_duplicate_log_lines(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        args = loop_agent.build_parser().parse_args(
            ["start", "--session", SID, "--dir", str(self.home.resolve())]
        )
        captured = {}

        class FakeProcess:
            pid = os.getpid()
            def poll(self):
                return None

        def capture(command, **kwargs):
            captured.update(kwargs)
            return FakeProcess()

        idle = loop_agent.load_state(SID)
        running = dict(
            idle,
            pid=os.getpid(),
            pid_started="test-signature",
            status="running",
        )
        states = iter([idle])

        def next_state(_sid):
            return next(states, running)

        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=capture
        ), mock.patch.object(
            loop_agent, "resolve_session", return_value=(SID, session)
        ), mock.patch.object(
            loop_agent, "load_state", side_effect=next_state
        ):
            rc = loop_agent.cmd_start(args)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["stderr"], loop_agent.subprocess.DEVNULL)
        self.assertEqual(captured["stdout"], loop_agent.subprocess.DEVNULL)

    def test_run_loop_stops_when_session_file_is_missing(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        session.unlink()
        with mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=AssertionError("should not resume")
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        final = loop_agent.load_state(SID)
        self.assertEqual(final["status"], "stopped")
        self.assertTrue(final["stop_requested"])

    def test_run_loop_retries_when_latest_terminal_event_is_aborted(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": "2026-09-13T00:00:05.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_aborted",
                            "reason": "interrupted",
                        },
                    }
                ) + "\n"
            )
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            raise OSError("boom")

        def mark_stop(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent.time, "sleep", side_effect=mark_stop
        ), mock.patch.object(
            loop_agent.subprocess, "Popen", side_effect=flaky
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(loop_agent.load_state(SID)["status"], "stopped")

    def test_run_loop_refreshes_state_updated_at_during_retries(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "exec",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        observed = []

        class FailProcess:
            returncode = 1

            def communicate(self, timeout=None):
                return "", "transient failure"

        def mark_stop(seconds):
            observed.append(loop_agent.load_state(SID)["last_attempt_at"])
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent.time, "sleep", side_effect=mark_stop
        ), mock.patch.object(
            loop_agent.subprocess, "Popen", return_value=FailProcess()
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertTrue(observed)
        self.assertIsNotNone(observed[0])
        self.assertEqual(loop_agent.load_state(SID)["last_error_kind"], "exit_nonzero")


    def test_run_loop_queue_transport_injects_and_waits_for_consumption(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        injected = []

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            with session.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-09-13T00:00:20.000Z",
                            "payload": {
                                "type": "user_message",
                                "message": prompt,
                            },
                        }
                    )
                    + "\n"
                )
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)
            return "Queued message 55555555-5555-5555-5555-555555555555 for thread x."

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(injected, ["继续"])
        final = loop_agent.load_state(SID)
        self.assertEqual(final["rounds"], 1)
        self.assertEqual(final["status"], "stopped")

    def test_queue_wait_yields_to_new_user_message(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 5000,
                "quiet": True,
            }
        )

        def fake_queue(session_id, prompt):
            with session.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "event_msg",
                            "timestamp": "2026-09-13T00:00:20.000Z",
                            "payload": {
                                "type": "user_message",
                                "message": "用户插话",
                            },
                        }
                    )
                    + "\n"
                )
            return "queued"


        calls = []

        def stop_after_two(seconds):
            calls.append(seconds)
            if len(calls) >= 2:
                state["stop_requested"] = True
                loop_agent.save_state(SID, state)

        loop_agent.save_state(SID, state)
        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_after_two):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(calls), 3)

    def test_run_loop_queue_transport_retries_on_queue_error(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        calls = []

        def flaky(session_id, prompt):
            calls.append(prompt)
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)
            raise RuntimeError("queue unavailable")

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=flaky
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        final = loop_agent.load_state(SID)
        self.assertEqual(final["last_error_kind"], "queue_error")

    def test_queue_wait_defers_while_other_user_items_are_queued(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        waiting_counts = iter([0, 1, 1, 0, 0])
        injected = []

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return "Queued message 22222222-2222-2222-2222-222222222222 for thread x."

        def fake_count(session_id, **kwargs):
            return next(waiting_counts, 0)

        def stop_on_sleep(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_user_message_count", side_effect=fake_count
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_on_sleep):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(injected, ["继续"])

    def test_queue_wait_does_not_reinject_same_prompt_when_queue_is_full(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        injected = []
        counts = iter([0, 0, 0, 0, 0, 0])

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return "Queued message 33333333-3333-3333-3333-333333333333 for thread x."

        def fake_count(session_id, **kwargs):
            return next(counts, 0)

        def stop_on_sleep(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_user_message_count", side_effect=fake_count
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_on_sleep):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(injected), 1)

    def test_queue_wait_ignores_matching_prompt_from_before_baseline(self):
        baseline = loop_agent.iso_to_epoch("2026-09-13T00:00:10.000Z")
        events = [
            {
                "type": "event_msg",
                "timestamp": "2026-09-13T00:00:03.000Z",
                "payload": {"type": "user_message", "message": "继续"},
            }
        ]
        self.assertEqual(
            loop_agent.queue_wait_outcome(events, "继续", set(), baseline), "waiting"
        )

    def test_queue_item_removal_marks_round_finished_without_reinjecting(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        injected = []
        # First look: our continuation is sitting in the queue. Second look:
        # the desktop consumed it. That pending -> absent transition is the
        # evidence that closes the round without re-injecting.
        item_id = "66666666-6666-6666-6666-666666666666"
        present = iter([{item_id}, set(), set(), set()])

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return f"Queued message {item_id} for thread x."

        def fake_ids(session_id):
            return next(present, set())

        def stop_after_round(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_item_ids_for_thread", side_effect=fake_ids
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_after_round):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(injected), 1)
        self.assertEqual(loop_agent.load_state(SID)["rounds"], 1)

    def test_never_observed_continuation_never_counts_as_a_round(self):
        """A continuation the queue never stored must not become a round.

        Regression test: the driver used to treat "my tracked id is gone" as
        proof of consumption even when the id was never pending, which let a
        lagging transcript spin out dozens of identical rounds per minute.
        """
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
                "unobserved_timeout_seconds": 0.001,
            }
        )
        loop_agent.save_state(SID, state)
        injected = []
        clock = iter([100.0 + i * 10.0 for i in range(500)])

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return f"Queued message {len(injected):08d}-6666-6666-6666-666666666666 for thread x."

        sleeps = []

        def stop_after_several(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 12:
                state["stop_requested"] = True
                loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_item_ids_for_thread", return_value=set()
        ), mock.patch.object(
            loop_agent.time, "time", side_effect=lambda: next(clock)
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_after_several):
            rc = loop_agent.run_loop(SID, session, state)
        final = loop_agent.load_state(SID)
        self.assertEqual(rc, 0)
        self.assertEqual(final["rounds"], 0)
        # Retrying is fine; inventing rounds and re-firing a fresh round for
        # every poll is not. Every retry must be the same continuation, and
        # the round counter must never advance on unproven consumption.
        self.assertTrue(injected)
        self.assertEqual(set(injected), {"继续"})
        self.assertEqual(final["sent_hashes"], [])

    def test_old_stop_message_before_arm_time_does_not_stop(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        with session.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-09-13T00:00:05.000Z",
                        "payload": {"type": "user_message", "message": "/stop"},
                    }
                )
                + "\n"
            )
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
                "armed_at": loop_agent.iso_to_epoch("2026-09-13T01:00:00.000Z"),
            }
        )
        loop_agent.save_state(SID, state)
        injected = []

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)
            return "Queued message 44444444-4444-4444-4444-444444444444 for thread x."

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(len(injected), 1)

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


    def test_user_queue_strictly_preempts_continuations(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        state = loop_agent.load_state(SID)
        state.update({
            "transport": "queue",
            "cwd": str(self.home.resolve()),
            "continuation": "继续",
            "poll_ms": 1,
            "quiet": True,
        })
        loop_agent.save_state(SID, state)
        injected = []

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return "Queued message 77777777-7777-7777-7777-777777777777 for thread x."

        def always_user_pending(session_id, **kwargs):
            return 1

        ticks = {"n": 0}

        def stop_after_ticks(seconds):
            ticks["n"] += 1
            if ticks["n"] >= 6:
                state["stop_requested"] = True
                loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_user_message_count", side_effect=always_user_pending
        ), mock.patch.object(
            loop_agent.time, "sleep", side_effect=stop_after_ticks
        ):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(injected, [])

    def test_cancel_queued_item_removes_our_pending_row(self):
        queue_db = self.home / "queue_1.sqlite"
        connection = sqlite3.connect(queue_db)
        connection.execute(
            "CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO queued_items VALUES (?, ?, ?, ?, ?, ?)",
            ("item-1", SID, "{}", 1, 1, 1),
        )
        connection.execute(
            "INSERT INTO queued_items VALUES (?, ?, ?, ?, ?, ?)",
            ("item-other", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "{}", 1, 1, 1),
        )
        connection.commit()
        connection.close()

        self.assertTrue(loop_agent.cancel_queued_item(SID, "item-1"))
        remaining = loop_agent.queued_item_ids_for_thread(SID)
        self.assertEqual(remaining, set())
        other = loop_agent.queued_item_ids_for_thread(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        )
        self.assertEqual(other, {"item-other"})

    def test_cancel_queued_item_reports_only_real_deletions(self):
        queue_db = self.home / "queue_1.sqlite"
        connection = sqlite3.connect(queue_db)
        connection.execute(
            "CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO queued_items VALUES (?, ?, ?, ?, ?, ?)",
            ("item-1", SID, "{}", 1, 1, 1),
        )
        connection.commit()
        connection.close()

        # A mismatched thread or a stale id deletes nothing, so it must not
        # report a withdrawal that never happened.
        self.assertFalse(loop_agent.cancel_queued_item(OTHER_SID, "item-1"))
        self.assertFalse(loop_agent.cancel_queued_item(SID, "missing"))
        self.assertTrue(loop_agent.cancel_queued_item(SID, "item-1"))
        # The row is gone now, so a second attempt is no longer a success.
        self.assertFalse(loop_agent.cancel_queued_item(SID, "item-1"))

    def test_queued_user_message_count_excludes_tracked_own_item(self):
        own_id = "77777777-7777-7777-7777-777777777777"
        items = [(own_id, "继续"), ("other-id", "真实用户")]
        with mock.patch.object(
            loop_agent, "queued_items_for_thread", return_value=items
        ):
            # Our own continuation must not count as a pending human message.
            self.assertEqual(loop_agent.queued_user_message_count(SID), 2)
            self.assertEqual(
                loop_agent.queued_user_message_count(SID, own_item_ids={own_id}),
                1,
            )

    def test_run_loop_does_not_stall_on_its_own_queued_continuation(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        own_id = "77777777-7777-7777-7777-777777777777"
        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
                "queued_item_ids": [own_id],
            }
        )
        loop_agent.save_state(SID, state)
        injected = []
        # The queue still holds the driver's own continuation and the
        # transcript never shows it consumed. The loop must keep working
        # instead of treating that item as a pending human message forever.

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            return f"Queued message {own_id} for thread x."

        def stop_after(seconds):
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(
            loop_agent, "queued_items_for_thread", return_value=[(own_id, "继续")]
        ), mock.patch.object(
            loop_agent,
            "queued_item_ids_for_thread",
            return_value={own_id},
        ), mock.patch.object(loop_agent.time, "sleep", side_effect=stop_after):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(injected, ["继续"])

    def test_reconcile_withdraws_stale_tracked_items(self):
        queue_db = self.home / "queue_1.sqlite"
        connection = sqlite3.connect(queue_db)
        connection.execute(
            "CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO queued_items VALUES (?, ?, ?, ?, ?, ?)",
            ("still-pending", SID, "{}", 1, 1, 1),
        )
        connection.commit()
        connection.close()

        state = loop_agent.load_state(SID)
        state["queued_item_ids"] = ["still-pending", "already-gone"]
        state["status"] = "stopped"
        loop_agent.save_state(SID, state)

        self.assertEqual(loop_agent.reconcile_tracked_queue_items(SID), set())
        self.assertEqual(loop_agent.queued_item_ids_for_thread(SID), set())
        reloaded = loop_agent.load_state(SID)
        self.assertEqual(reloaded.get("queued_item_ids") or [], [])

    def test_stop_withdraws_pending_queued_continuation(self):
        session = session_file(self.sessions_root, SID, str(self.home.resolve()))
        queue_db = self.home / "queue_1.sqlite"
        connection = sqlite3.connect(queue_db)
        connection.execute(
            "CREATE TABLE queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL, payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)"
        )
        connection.commit()
        connection.close()

        state = loop_agent.load_state(SID)
        state.update(
            {
                "transport": "queue",
                "cwd": str(self.home.resolve()),
                "continuation": "继续",
                "poll_ms": 1,
                "quiet": True,
            }
        )
        loop_agent.save_state(SID, state)
        injected = []
        item_id = "99999999-9999-9999-9999-999999999999"

        def fake_queue(session_id, prompt):
            injected.append(prompt)
            connection = sqlite3.connect(queue_db)
            connection.execute(
                "INSERT OR REPLACE INTO queued_items VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, SID, "{}", 1, 1, 1),
            )
            connection.commit()
            connection.close()
            state["stop_requested"] = True
            loop_agent.save_state(SID, state)
            return f"Queued message {item_id} for thread x."

        with mock.patch.object(
            loop_agent, "inject_via_queue", side_effect=fake_queue
        ), mock.patch.object(loop_agent.time, "sleep"):
            rc = loop_agent.run_loop(SID, session, state)
        self.assertEqual(rc, 0)
        self.assertEqual(injected, ["继续"])
        self.assertEqual(loop_agent.queued_item_ids_for_thread(SID), set())
        final = loop_agent.load_state(SID)
        self.assertEqual(final.get("queued_item_ids") or [], [])
        self.assertTrue(final.get("stop_requested"))

    def test_parse_queued_item_id_extracts_uuid_and_ignores_junk(self):
        item_id = "01a0a289-38e9-7672-8e04-9fd60b3cff94"
        self.assertEqual(
            loop_agent.parse_queued_item_id(
                f"Queued message {item_id} for thread abc."
            ),
            item_id,
        )
        # The driver relies on this parse to track its own continuation;
        # unparsable output must stay None rather than inventing an id.
        for text in ("", "unrelated output", "Queued message  for thread x."):
            self.assertIsNone(loop_agent.parse_queued_item_id(text), text)

    def test_parse_until_accepts_iso_and_epoch_and_rejects_junk(self):
        self.assertIsNone(loop_agent.parse_until(None))
        self.assertIsNone(loop_agent.parse_until(""))
        self.assertEqual(loop_agent.parse_until("1789257600"), 1789257600.0)
        self.assertEqual(
            loop_agent.parse_until("2026-09-13T00:00:00.000Z"),
            1789257600.0,
        )
        with self.assertRaises(ValueError):
            loop_agent.parse_until("not-a-time")

    def test_queued_payload_text_collects_real_user_input(self):
        payload = json.dumps(
            {
                "UserInput": {
                    "content": [
                        {"type": "text", "text": "继续", "text_elements": []}
                    ],
                    "client_id": "abc",
                }
            }
        )
        self.assertEqual(loop_agent.queued_payload_text(payload), "继续")
        # Malformed payloads degrade to an empty string instead of raising.
        for bad in (None, "not json", "123"):
            self.assertEqual(loop_agent.queued_payload_text(bad), "")

    def test_queue_database_path_prefers_newest_and_handles_none(self):
        self.assertIsNone(loop_agent.queue_database_path())
        older = self.home / "queue_1.sqlite"
        newer = self.home / "queue_2.sqlite"
        older.write_text("")
        newer.write_text("")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        self.assertEqual(loop_agent.queue_database_path(), newer)

    def test_prune_queued_item_ids_keeps_only_still_pending(self):
        tracked = {"still-there", "consumed"}
        with mock.patch.object(
            loop_agent,
            "queued_item_ids_for_thread",
            return_value={"still-there"},
        ):
            self.assertEqual(
                loop_agent.prune_queued_item_ids(SID, tracked), {"still-there"}
            )
        # An unreadable queue keeps the tracked set so nothing is forgotten.
        with mock.patch.object(
            loop_agent, "queued_item_ids_for_thread", return_value=None
        ):
            self.assertEqual(loop_agent.prune_queued_item_ids(SID, tracked), tracked)

    def test_orphaned_continuation_items_finds_only_untracked_matches(self):
        items = [
            ("tracked", "继续"),
            ("orphan", "继续"),
            ("human", "帮我改个 bug"),
        ]
        with mock.patch.object(
            loop_agent, "queued_items_for_thread", return_value=items
        ):
            found = loop_agent.orphaned_continuation_items(
                SID, "继续", {"tracked"}
            )
        self.assertEqual(found, [("orphan", "继续")])

    def test_session_archived_or_deleted_detects_archive_move(self):
        active = self.sessions_root / f"rollout-x-{SID}.jsonl"
        active.write_text("", encoding="utf-8")
        self.assertFalse(loop_agent.session_archived_or_deleted(SID, active))

        # Archiving moves the rollout into archived_sessions/; the loop must
        # still stop even though nothing was deleted outright.
        archived_root = self.home / "archived_sessions"
        archived_root.mkdir(parents=True, exist_ok=True)
        moved = archived_root / active.name
        active.rename(moved)
        self.assertTrue(loop_agent.session_archived_or_deleted(SID, active))

    def test_session_archived_or_deleted_detects_deletion(self):
        missing = self.sessions_root / f"rollout-gone-{SID}.jsonl"
        self.assertTrue(loop_agent.session_archived_or_deleted(SID, missing))
        tracked = {"still-there", "consumed"}
        with mock.patch.object(
            loop_agent,
            "queued_item_ids_for_thread",
            return_value={"still-there"},
        ):
            self.assertEqual(
                loop_agent.prune_queued_item_ids(SID, tracked), {"still-there"}
            )
        # An unreadable queue keeps the tracked set so nothing is forgotten.
        with mock.patch.object(
            loop_agent, "queued_item_ids_for_thread", return_value=None
        ):
            self.assertEqual(loop_agent.prune_queued_item_ids(SID, tracked), tracked)

    def test_queue_wait_outcome_treats_own_prompt_as_observed(self):
        baseline = loop_agent.iso_to_epoch("2026-09-13T00:00:10.000Z")
        events = [
            {
                "type": "event_msg",
                "timestamp": "2026-09-13T00:00:20.000Z",
                "payload": {"type": "user_message", "message": "继续"},
            }
        ]
        self.assertEqual(
            loop_agent.queue_wait_outcome(events, "继续", set(), baseline),
            "observed",
        )
        # Our own prompt is never mistaken for an interrupting human.
        self.assertNotEqual(
            loop_agent.queue_wait_outcome(
                events, "继续", {loop_agent.sha256_text("继续")}, baseline
            ),
            "user_pending",
        )

    def test_queue_wait_outcome_reports_real_user_message(self):
        baseline = loop_agent.iso_to_epoch("2026-09-13T00:00:10.000Z")
        events = [
            {
                "type": "event_msg",
                "timestamp": "2026-09-13T00:00:20.000Z",
                "payload": {"type": "user_message", "message": "插一句"},
            }
        ]
        self.assertEqual(
            loop_agent.queue_wait_outcome(events, "继续", set(), baseline),
            "user_pending",
        )


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
