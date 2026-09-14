#!/usr/bin/env python3
"""Endless loop driver for Codex, modeled after dsh-loop-agent.

The driver keeps one Codex session alive: after every completed assistant
turn it resumes that session with a continuation prompt, retries failures
with exponential backoff, and never runs ahead of a real user message.

Usage:
  codex-loop-agent start [--session <uuid>] [--dir <dir>] [options]
  codex-loop-agent stop --session <uuid> | --last | --all
  codex-loop-agent status [--all | --session <uuid> | --last] [--json]
  codex-loop-agent config show | set <key> <value> | enable | disable
  codex-loop-agent logs [--session <uuid> | --last] [--lines N]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_CONTINUATION = "继续，并深度检查暗病，遇到暗病和缺陷就修复"
CONFIG_FILE = ".codex-loop-agent.json"
RUNTIME_DIR = ".codex-loop-agent"
SESSIONS_DIR = "sessions"
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
STOP_PATTERNS = [
    r"/stop\b",
    r"/forever\s+stop\b",
    r"stop\s+(the\s+)?loop\b",
    r"停止无尽模式",
    r"停止无限循环",
    r"退出无尽模式",
    r"关闭无尽模式",
    r"停止循环",
    r"停一下",
    r"不要再继续",
    r"不要再跑",
    r"别继续",
]
CONTEXT_ERROR_MARKERS = (
    "context",
    "token",
    "too long",
    "allocation error",
    "not enough memory",
    "context_length_exceeded",
)
CONTINUATION_VARS = re.compile(r"{{\s*(lastAnswer|last_answer|round|task)\s*}}")

_CURRENT_SESSION: str | None = None
_CURRENT_PROCESS: subprocess.Popen | None = None


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def global_config_path() -> Path:
    return codex_home() / CONFIG_FILE


def load_global_config() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "disabled": False,
        "continuation": DEFAULT_CONTINUATION,
        "updatedAt": now_iso(),
    }
    payload = read_json(global_config_path())
    if not payload:
        return defaults
    merged = dict(defaults)
    merged.update(payload)
    return merged


def save_global_config(payload: dict[str, Any]) -> Path:
    payload["updatedAt"] = now_iso()
    path = global_config_path()
    write_json(path, payload)
    return path


def runtime_dir() -> Path:
    path = codex_home() / RUNTIME_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_path(session_id: str) -> Path:
    return runtime_dir() / f"{session_id}.json"


def log_path(session_id: str) -> Path:
    return runtime_dir() / f"{session_id}.log"


def load_state(session_id: str) -> dict[str, Any]:
    payload = read_json(state_path(session_id)) or {}
    return {
        "version": 1,
        "session_id": session_id,
        "session_file": None,
        "cwd": None,
        "pid": None,
        "status": "unknown",
        "rounds": 0,
        "started_at": None,
        "pid_started": None,
        "updated_at": None,
        "stop_requested": False,
        "continuation": None,
        "max_rounds": 0,
        "until": None,
        "initial_backoff_ms": 1000,
        "max_backoff_ms": 32000,
        "backoff_factor": 2.0,
        "poll_ms": 2000,
        "timeout_seconds": 0,
        "quiet": False,
        "sent_hashes": [],
        "last_completion_ts": 0.0,
        **payload,
    }


def save_state(session_id: str, payload: dict[str, Any]) -> Path:
    payload["updated_at"] = now_iso()
    path = state_path(session_id)
    write_json(path, payload)
    return path


def log_line(session_id: str, message: str, quiet: bool = False) -> None:
    line = f"{now_iso()} {message}"
    try:
        with log_path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass
    if not quiet:
        print(line, file=sys.stderr)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ps_field(pid: int | None, field: str) -> str | None:
    if not pid or pid <= 0:
        return None
    read_fd, write_fd = os.pipe()
    child: int | None = None
    chunks: list[bytes] = []
    try:
        child = os.fork()
        if child == 0:
            try:
                os.close(read_fd)
                os.dup2(write_fd, 1)
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 2)
                os.execvp("ps", ["ps", "-p", str(pid), "-o", field])
            except BaseException:
                pass
            os._exit(127)
        os.close(write_fd)
        write_fd = -1
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        if child is not None and child != 0:
            try:
                os.waitpid(child, 0)
            except OSError:
                pass
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    value = b"".join(chunks).decode("utf-8", "ignore").strip()
    return value or None


def process_command_matches(pid: int | None) -> bool:
    command = _ps_field(pid, "command=")
    if not command:
        return False
    script = os.path.abspath(__file__)
    return script in command or os.path.basename(script) in command


def process_start_signature(pid: int | None) -> str | None:
    return _ps_field(pid, "lstart=")


def is_same_process(pid: int | None, pid_started: str | None) -> bool:
    if not is_process_alive(pid):
        return False
    if not pid_started:
        # Older state files predate pid_started; keep working, but only trust
        # a live PID if we can still confirm it is the loop driver command.
        return process_command_matches(pid)
    signature = process_start_signature(pid)
    if not signature:
        return True
    return str(pid_started) == signature


def iso_to_epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return float(text)
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.timestamp()
        except ValueError:
            return 0.0
    return 0.0


def parse_until(value: str | None) -> float | None:
    if not value:
        return None
    epoch = iso_to_epoch(value)
    if not epoch:
        raise ValueError(
            f"cannot parse --until value {value!r}; use ISO time or a unix epoch"
        )
    return epoch


# --------------------------------------------------------------------------
# Session log parsing
# --------------------------------------------------------------------------

def event_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def read_events(session_file: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with session_file.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return events


def session_id_from_path(path: Path) -> str | None:
    match = UUID_RE.search(path.name)
    return match.group(0) if match else None


def session_cwd(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "turn_context":
                    continue
                payload = event_payload(event)
                if payload and payload.get("cwd"):
                    return payload["cwd"]
    except OSError:
        return None
    return None


def resolve_session(
    session_id: str | None,
    cwd: str | None,
    last: bool,
) -> tuple[str, Path]:
    sessions_root = codex_home() / SESSIONS_DIR
    if not sessions_root.is_dir():
        raise RuntimeError(f"no Codex sessions directory at {sessions_root}")

    files = sorted(
        sessions_root.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise RuntimeError(f"no Codex session files found under {sessions_root}")

    if session_id:
        normalized = session_id.strip().lower()
        for path in files:
            if session_id_from_path(path) == normalized:
                return normalized, path
        raise RuntimeError(f"session {session_id} not found under {sessions_root}")

    if cwd:
        matching = [p for p in files if session_cwd(p) == cwd]
        if matching:
            path = matching[0]
            found = session_id_from_path(path)
            if found:
                return found, path
        if last:
            raise RuntimeError(f"no session found for cwd {cwd}")

    if last or not cwd:
        path = files[0]
        found = session_id_from_path(path)
        if found:
            return found, path

    raise RuntimeError(
        "could not resolve a session; pass --session <uuid> or --last explicitly"
    )


def event_user_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "event_msg":
            continue
        payload = event_payload(event)
        if not payload or payload.get("type") != "user_message":
            continue
        text = payload.get("message")
        if text is None:
            text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        timestamp = iso_to_epoch(event.get("timestamp")) or iso_to_epoch(
            payload.get("timestamp")
        )
        messages.append(
            {"ts": timestamp, "text": text.strip(), "sha": sha256_text(text.strip())}
        )
    return messages


def last_completion_ts(events: list[dict[str, Any]]) -> float:
    latest = 0.0
    for event in events:
        payload = event_payload(event)
        if not payload:
            continue
        if payload.get("type") != "task_complete":
            continue
        timestamp = iso_to_epoch(event.get("timestamp")) or iso_to_epoch(
            payload.get("timestamp")
        )
        latest = max(latest, timestamp)
    return latest


def last_aborted_ts(events: list[dict[str, Any]]) -> float:
    latest = 0.0
    for event in events:
        payload = event_payload(event)
        if not payload:
            continue
        if payload.get("type") != "turn_aborted":
            continue
        timestamp = iso_to_epoch(event.get("timestamp")) or iso_to_epoch(
            payload.get("timestamp")
        )
        latest = max(latest, timestamp)
    return latest


def last_assistant_text(events: list[dict[str, Any]], max_chars: int = 4000) -> str:
    for event in reversed(events):
        payload = event_payload(event)
        if not payload:
            continue
        chunks: list[str] = []
        if event.get("type") == "response_item" and payload.get("type") == "message":
            if payload.get("role") != "assistant":
                continue
            content = payload.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        chunks.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            chunks.append(text)
        elif event.get("type") == "event_msg" and payload.get("type") == "agent_message":
            text = payload.get("message") or payload.get("text")
            if isinstance(text, str):
                chunks.append(text)
        if chunks:
            return "\n".join(chunks).strip()[:max_chars]
    return ""


def original_task_text(messages: list[dict[str, Any]], max_chars: int = 1000) -> str:
    for message in messages:
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = text.strip()
        if is_stop_message(stripped) or stripped.lower().startswith("/forever"):
            continue
        return stripped[:max_chars]
    return ""


def render_continuation(
    template: str,
    last_answer: str = "",
    round_number: int = 0,
    task: str = "",
) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1).lower()
        if key in ("lastanswer", "last_answer"):
            return last_answer
        if key == "round":
            return str(round_number)
        return task

    return CONTINUATION_VARS.sub(replace, template)


def is_stop_message(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in STOP_PATTERNS)


def pending_human_message(
    messages: list[dict[str, Any]],
    sent_hashes: set[str],
    last_completion: float,
) -> dict[str, Any] | None:
    humans = [m for m in messages if m["sha"] not in sent_hashes]
    loops = [m for m in messages if m["sha"] in sent_hashes]
    if not humans:
        return None
    latest = humans[-1]
    latest_loop_ts = loops[-1]["ts"] if loops else 0.0
    if latest["ts"] > latest_loop_ts and latest["ts"] > last_completion:
        return latest
    return None


# --------------------------------------------------------------------------
# Driver loop
# --------------------------------------------------------------------------

def build_codex_command(
    session_id: str,
    prompt: str,
) -> list[str]:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found on PATH")
    command = [
        codex,
        "exec",
        "resume",
        session_id,
        prompt,
        "--skip-git-repo-check",
        "--json",
    ]
    return command


def run_loop(session_id: str, session_file: Path, state: dict[str, Any]) -> int:
    global _CURRENT_SESSION
    _CURRENT_SESSION = session_id

    cwd = Path(state.get("cwd") or os.getcwd()).expanduser().resolve()
    continuation = state.get("continuation") or load_global_config().get(
        "continuation", DEFAULT_CONTINUATION
    )
    max_rounds = int(state.get("max_rounds") or 0)
    until = parse_until(state.get("until"))
    initial_backoff = float(state.get("initial_backoff_ms") or 1000) / 1000.0
    max_backoff = float(state.get("max_backoff_ms") or 32000) / 1000.0
    backoff_factor = float(state.get("backoff_factor") or 2.0)
    poll_seconds = float(state.get("poll_ms") or 2000) / 1000.0
    timeout = float(state.get("timeout_seconds") or 0)
    quiet = bool(state.get("quiet", False))

    state["status"] = "running"
    state["pid"] = os.getpid()
    state["pid_started"] = process_start_signature(os.getpid())
    save_state(session_id, state)

    sent_hashes = set(state.get("sent_hashes") or [])
    rounds = int(state.get("rounds") or 0)
    last_completion = float(state.get("last_completion_ts") or 0.0)
    backoff = initial_backoff
    failed_prompt: str | None = None

    log_line(session_id, f"loop started for session {session_id}", quiet)

    def persist_progress(status: str = "running") -> None:
        state["status"] = status
        state["sent_hashes"] = sorted(sent_hashes)
        state["rounds"] = rounds
        state["last_completion_ts"] = last_completion
        save_state(session_id, state)

    def note_error(kind: str, detail: str) -> None:
        state["last_error_kind"] = kind
        state["last_error"] = detail[-800:]
        state["last_attempt_at"] = now_iso()
        persist_progress()

    def stop_loop(reason: str) -> int:
        state["stop_requested"] = True
        state["stop_reason"] = reason
        persist_progress("stopped")
        log_line(session_id, f"loop stopped: {reason}", quiet)
        return 0

    while True:
        current_state = load_state(session_id)
        if current_state.get("stop_requested"):
            return stop_loop("stop requested via state file")

        if load_global_config().get("disabled"):
            persist_progress("disabled")
            log_line(
                session_id,
                "global endless-loop switch is off; use `config enable` to turn it back on",
                quiet,
            )
            return 0

        if max_rounds and rounds >= max_rounds:
            persist_progress("completed")
            log_line(session_id, f"loop completed after {rounds} round(s)", quiet)
            return 0

        if until and time.time() >= until:
            return stop_loop("--until deadline reached")

        events = read_events(session_file)
        if not session_file.exists():
            return stop_loop("session file was archived or deleted")
        last_completion = max(last_completion, last_completion_ts(events))
        messages = event_user_messages(events)

        humans = [m for m in messages if m["sha"] not in sent_hashes]
        loops = [m for m in messages if m["sha"] in sent_hashes]
        latest_loop_ts = loops[-1]["ts"] if loops else 0.0
        if humans and humans[-1]["ts"] > latest_loop_ts:
            latest_human = humans[-1]
            if is_stop_message(latest_human["text"]):
                return stop_loop("user sent a stop message")
            if latest_human["ts"] > last_completion:
                log_line(
                    session_id,
                    "a real user message is pending; waiting for that turn to finish",
                    quiet,
                )
                persist_progress()
                time.sleep(poll_seconds)
                continue

        last_answer = last_assistant_text(events)
        task = original_task_text(messages)
        prompt = (
            failed_prompt
            if failed_prompt is not None
            else render_continuation(continuation, last_answer, rounds + 1, task)
        )

        log_line(session_id, f"round {rounds + 1}: resuming session", quiet)
        state["last_attempt_at"] = now_iso()
        persist_progress()
        process: subprocess.Popen | None = None
        try:
            command = build_codex_command(session_id, prompt)
            global _CURRENT_PROCESS
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                cwd=cwd,
                stderr=subprocess.PIPE,
                text=True,
            )
            _CURRENT_PROCESS = process
            try:
                stdout, stderr = process.communicate(
                    timeout=timeout if timeout > 0 else None
                )
            finally:
                _CURRENT_PROCESS = None
            result = subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        except subprocess.TimeoutExpired as exc:
            error = f"codex resume timed out after {timeout}s"
            if exc.stderr:
                error += f": {str(exc.stderr)[-400:]}"
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            log_line(session_id, error, quiet)
            note_error("timeout", error)
            failed_prompt = prompt
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * backoff_factor)
            continue
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait()
                except OSError:
                    pass
            detail = str(exc).strip() or exc.__class__.__name__
            log_line(
                session_id,
                f"codex resume failed ({exc.__class__.__name__}): {detail}",
                quiet,
            )
            failed_prompt = prompt
            note_error("spawn_error", detail)
            log_line(
                session_id,
                f"retrying same continuation in {int(backoff * 1000)}ms",
                quiet,
            )
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * backoff_factor)
            continue

        if result.returncode == 0:
            rounds += 1
            sent_hashes.add(sha256_text(prompt))
            last_completion = max(last_completion, time.time())
            backoff = initial_backoff
            failed_prompt = None
            state.pop("last_error_kind", None)
            state.pop("last_error", None)
            persist_progress()
            log_line(session_id, f"round {rounds} finished", quiet)
            time.sleep(poll_seconds)
            continue

        error_tail = (result.stderr or result.stdout or "").strip()
        lowered = error_tail.lower()
        error_kind = "exit_nonzero"
        if "already has an active writer" in lowered:
            error_kind = "active_writer"
        elif any(marker in lowered for marker in CONTEXT_ERROR_MARKERS):
            error_kind = "context_pressure"
        summary = error_tail[-400:].replace("\n", " ")
        log_line(
            session_id,
            f"round failed (exit {result.returncode}, {error_kind}): {summary}",
            quiet,
        )
        if "already has an active writer" in lowered:
            log_line(
                session_id,
                "the Codex app currently holds this thread's writer lock; "
                "CLI resume cannot run concurrently, so the loop will keep retrying",
                quiet,
            )
        if any(marker in lowered for marker in CONTEXT_ERROR_MARKERS):
            log_line(
                session_id,
                "context/token pressure detected; the Codex app can compact this session before the next retry",
                quiet,
            )
        failed_prompt = prompt
        note_error(error_kind, error_tail)
        log_line(
            session_id,
            f"retrying same continuation in {int(backoff * 1000)}ms",
            quiet,
        )
        time.sleep(backoff)
        backoff = min(max_backoff, backoff * backoff_factor)

    return 0


def _signal_handler(signum: int, frame: Any) -> None:
    if _CURRENT_SESSION:
        state = load_state(_CURRENT_SESSION)
        state["stop_requested"] = True
        state["status"] = "stopped"
        save_state(_CURRENT_SESSION, state)
    if _CURRENT_PROCESS and _CURRENT_PROCESS.poll() is None:
        try:
            _CURRENT_PROCESS.terminate()
        except OSError:
            pass
    raise SystemExit(0)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    requested_cwd = Path(args.dir or os.getcwd()).expanduser().resolve()
    dir_from_session = bool(getattr(args, "dir_from_session", False))
    explicit_dir = args.dir is not None and not dir_from_session
    selector_only = bool(args.session or args.last) and not explicit_dir
    resolve_cwd = None if selector_only else str(requested_cwd)
    session_id, session_file = resolve_session(
        args.session,
        resolve_cwd,
        args.last,
    )
    recorded_cwd = session_cwd(session_file)
    if args.dir is not None and not dir_from_session:
        selected_cwd = requested_cwd
    elif recorded_cwd:
        selected_cwd = Path(recorded_cwd).expanduser().resolve()
    else:
        selected_cwd = requested_cwd
    cwd = selected_cwd

    existing = load_state(session_id)
    if (
        existing.get("pid")
        and is_same_process(existing.get("pid"), existing.get("pid_started"))
        and not args.foreground
    ):
        print(
            f"loop already running for session {session_id} "
            f"(pid {existing['pid']}); use `stop` first",
            file=sys.stderr,
        )
        return 1

    if load_global_config().get("disabled"):
        print(
            "global endless-loop switch is off; run `codex-loop-agent config enable` first",
            file=sys.stderr,
        )
        return 1

    state = load_state(session_id)
    state.update(
        {
            "session_file": str(session_file),
            "cwd": str(cwd),
            "status": "starting",
            "started_at": now_iso(),
            "stop_requested": False,
            "continuation": args.continuation
            or load_global_config().get("continuation", DEFAULT_CONTINUATION),
            "max_rounds": int(args.max_rounds or 0),
            "until": args.until,
            "initial_backoff_ms": float(args.initial_backoff_ms),
            "max_backoff_ms": float(args.max_backoff_ms),
            "backoff_factor": float(args.backoff_factor),
            "poll_ms": float(args.poll_ms),
            "timeout_seconds": float(args.timeout_seconds or 0),
            "quiet": bool(args.quiet),
        }
    )
    save_state(session_id, state)

    if args.foreground:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        return run_loop(session_id, session_file, state)

    command = [
        sys.executable,
        os.path.abspath(__file__),
        "start",
        "--session",
        session_id,
        "--dir",
        str(cwd),
        "--foreground",
        "--continuation",
        state["continuation"],
        "--max-rounds",
        str(state["max_rounds"]),
        "--initial-backoff-ms",
        str(state["initial_backoff_ms"]),
        "--max-backoff-ms",
        str(state["max_backoff_ms"]),
        "--backoff-factor",
        str(state["backoff_factor"]),
        "--poll-ms",
        str(state["poll_ms"]),
        "--timeout-seconds",
        str(state["timeout_seconds"]),
    ]
    if state.get("until"):
        command += ["--until", state["until"]]
    if state.get("quiet"):
        command.append("--quiet")

    command.append("--dir-from-session")

    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        current = load_state(session_id)
        if current.get("pid") and is_same_process(
            current.get("pid"), current.get("pid_started")
        ):
            print(f"endless loop started for session {session_id} (pid {current['pid']})")
            print(f"log: {log_path(session_id)}")
            print("stop with: `codex-loop-agent stop --session <session-id>` or send /stop in the thread")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.2)

    print("endless loop failed to start; latest log:", file=sys.stderr)
    tail = []
    try:
        with log_path(session_id).open(encoding="utf-8", errors="ignore") as handle:
            tail = handle.readlines()[-10:]
    except OSError:
        pass
    for line in tail:
        print(line.rstrip(), file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    if args.all:
        targets = [
            (path.stem, load_state(path.stem))
            for path in sorted(runtime_dir().glob("*.json"))
        ]
    else:
        cwd = str(Path(args.dir).expanduser().resolve()) if args.dir else None
        session_id, _ = resolve_session(args.session, cwd, args.last)
        targets = [(session_id, load_state(session_id))]

    if not targets:
        print("no active endless loops", file=sys.stderr)
        return 0

    for session_id, state in targets:
        state["stop_requested"] = True
        state["status"] = "stopped"
        save_state(session_id, state)
        pid = state.get("pid")
        if pid and is_same_process(pid, state.get("pid_started")):
            pid_int = int(pid)
            os.kill(pid_int, signal.SIGTERM)
            deadline = time.time() + (0.5 if args.force else 3.0)
            while time.time() < deadline and is_process_alive(pid_int):
                try:
                    waited, _status = os.waitpid(pid_int, os.WNOHANG)
                    if waited:
                        break
                except ChildProcessError:
                    if not is_process_alive(pid_int):
                        break
                time.sleep(0.05)
            if args.force and is_process_alive(pid_int):
                os.kill(pid_int, signal.SIGKILL)
                try:
                    os.waitpid(pid_int, 0)
                except ChildProcessError:
                    pass
            print(f"stopped loop {session_id} (pid {pid})")
        else:
            print(f"marked loop {session_id} as stopped")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if args.dir:
        cwd = str(Path(args.dir).expanduser().resolve())
        session_id, _ = resolve_session(args.session, cwd, args.last)
        targets = [(session_id, load_state(session_id))]
    elif args.all or not (args.session or args.last):
        targets = [
            (path.stem, load_state(path.stem))
            for path in sorted(runtime_dir().glob("*.json"))
        ]
    else:
        session_id, _ = resolve_session(args.session, None, args.last)
        targets = [(session_id, load_state(session_id))]

    config = load_global_config()
    rows = []
    for session_id, state in targets:
        rows.append(
            {
                "session": session_id,
                "running": is_same_process(
                    state.get("pid"), state.get("pid_started")
                ),
                "status": state.get("status"),
                "rounds": state.get("rounds"),
                "pid": state.get("pid"),
                "continuation": state.get("continuation")
                or config.get("continuation"),
                "started_at": state.get("started_at"),
                "updated_at": state.get("updated_at"),
            }
        )

    if args.json:
        print(
            json.dumps(
                {"global": config, "loops": rows}, ensure_ascii=False, indent=2
            )
        )
        return 0

    print(f"global switch: {'OFF' if config.get('disabled') else 'ON'}")
    print(f"default continuation: {config.get('continuation')}")
    if not rows:
        print("no sessions registered with the endless loop")
        return 0
    print(f"{'SESSION':38s} {'STATUS':10s} {'ROUNDS':6s} {'PID':8s} CONTINUATION")
    for row in rows:
        print(
            f"{row['session']:38s} {(row['status'] or 'unknown')[:10]:10s} "
            f"{str(row['rounds']):6s} {str(row['pid'] or '-'):8s} {row['continuation'] or ''}"
        )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = global_config_path()
    config = load_global_config()
    if args.action == "show":
        print(json.dumps(config, ensure_ascii=False, indent=2))
        print(f"# config file: {path}")
        return 0
    if args.action == "enable":
        config["disabled"] = False
    elif args.action == "disable":
        config["disabled"] = True
    elif args.action == "set":
        key = args.key
        value: Any = args.value
        if key not in ("continuation", "disabled"):
            raise SystemExit(
                f"unknown config key {key!r}; expected continuation or disabled"
            )
        if key == "continuation":
            value = str(value).strip()
            if not value:
                raise SystemExit("`continuation` cannot be empty")
        if key == "disabled":
            lowered = str(value).strip().lower()
            if lowered in ("true", "1", "on", "yes"):
                value = True
            elif lowered in ("false", "0", "off", "no", "empty"):
                value = False
            else:
                raise SystemExit(
                    "`disabled` expects true/false/on/off; use `config enable` or `config disable`"
                )
        config[key] = value
    saved = save_global_config(config)
    print(json.dumps(config, ensure_ascii=False, indent=2))
    print(f"# saved to {saved}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cwd = str(Path(args.dir).expanduser().resolve()) if args.dir else None
    session_id, _ = resolve_session(args.session, cwd, args.last)
    path = log_path(session_id)
    if not path.exists():
        print(f"no log file yet: {path}", file=sys.stderr)
        return 1
    with path.open(encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()[-max(1, int(args.lines)) :]
    sys.stdout.write("".join(lines))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-loop-agent", description=__doc__)
    parser.add_argument("--version", action="version", version="codex-loop-agent 1.0.20260914")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start an endless loop for a Codex session")
    start.add_argument("--session", help="session UUID; omit to auto-resolve by --dir")
    start.add_argument("--last", action="store_true", help="use the newest session")
    start.add_argument(
        "--dir",
        default=None,
        dest="dir",
        help="working directory for the loop",
    )
    start.add_argument("--continuation", help="continuation prompt template")
    start.add_argument("--max-rounds", type=int, default=0, help="stop after N continuation rounds (0 = unlimited)")
    start.add_argument("--until", help="stop at an ISO time or unix epoch")
    start.add_argument("--initial-backoff-ms", type=float, default=1000)
    start.add_argument("--max-backoff-ms", type=float, default=32000)
    start.add_argument("--backoff-factor", type=float, default=2.0)
    start.add_argument("--poll-ms", type=float, default=2000, help="user-priority poll interval")
    start.add_argument("--timeout-seconds", type=float, default=0, help="max seconds per resume")
    start.add_argument("--quiet", action="store_true")
    start.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    start.add_argument("--dir-from-session", action="store_true", help=argparse.SUPPRESS)

    stop = subparsers.add_parser("stop", help="stop an endless loop")
    stop.add_argument("--session")
    stop.add_argument("--last", action="store_true")
    stop.add_argument("--all", action="store_true")
    stop.add_argument("--force", action="store_true", help="SIGKILL if the driver does not exit")
    stop.add_argument("--dir", help="resolve the session by working directory")

    status = subparsers.add_parser("status", help="show loop state")
    status.add_argument("--session")
    status.add_argument("--last", action="store_true")
    status.add_argument("--all", action="store_true")
    status.add_argument("--json", action="store_true")
    status.add_argument("--dir", help="resolve the session by working directory")

    config = subparsers.add_parser("config", help="manage the global endless-loop switch")
    config_sub = config.add_subparsers(dest="action", required=True)
    config_sub.add_parser("show", help="show global config")
    config_sub.add_parser("enable", help="turn the global switch ON")
    config_sub.add_parser("disable", help="turn the global switch OFF")
    config_set = config_sub.add_parser("set", help="set a global config value")
    config_set.add_argument("key")
    config_set.add_argument("value")

    logs = subparsers.add_parser("logs", help="print the loop driver log")
    logs.add_argument("--session")
    logs.add_argument("--last", action="store_true")
    logs.add_argument("--lines", type=int, default=40)
    logs.add_argument("--dir", help="resolve the session by working directory")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            return cmd_start(args)
        if args.command == "stop":
            return cmd_stop(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "config":
            return cmd_config(args)
        if args.command == "logs":
            return cmd_logs(args)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
