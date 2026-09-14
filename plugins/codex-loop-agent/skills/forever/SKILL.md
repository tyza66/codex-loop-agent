---
name: forever
description: Activate, stop, and manage Codex Endless Loop mode. Use when the user types /forever, asks for 无尽模式 / 无限循环 / 一直继续 / 持续跑 / 不要停, or asks to 停止无尽模式 / 停止无限循环 / 停止循环 / /stop.
---

# Endless Loop (`/forever`)

Endless Loop is an opt-in same-session loop, modeled after
[dsh-loop-agent](https://github.com/tyza66/dsh-loop-agent). The bundled
`scripts/loop-agent.py` driver watches the current Codex session; after every
completed assistant turn it resumes the same session with a configurable
continuation prompt. It never runs ahead of a real user message, retries
failures with exponential backoff, and keeps going until the user stops it.

## Activation

1. Locate the bundled driver:

```sh
SCRIPT=$(find ~/.codex/plugins -path '*/codex-loop-agent/scripts/loop-agent.py' -print -quit)
python3 "$SCRIPT" status --all
```

2. Make sure the global switch is ON:

```sh
python3 "$SCRIPT" config enable
```

3. Start the endless loop for the current thread. The script auto-resolves the
   newest Codex session whose working directory matches, so running it from the
   current workspace is usually enough:

```sh
python3 "$SCRIPT" start --dir "$PWD"
```

If multiple threads share the same directory, pass the explicit session UUID:

```sh
python3 "$SCRIPT" start --session <session-uuid> --dir "$PWD"
```

4. Tell the user the loop is active, show how many rounds have run, and mention
   that a real message or `/stop` stops it.

## Behavior inside the loop

While the driver is active, injected continuations appear in the session as
normal-looking messages. Treat them as task directives from the loop driver,
not as a brand-new request from the user:

- Keep working on the original goal after each answer; do not stop just because
  one subtask happens to be done.
- Always answer real user messages first when they appear. Real user messages
  are the ones typed by the person in the thread, not the injected
  continuation prompts.
- If the user sends `/stop`, 停止无尽模式, or an equivalent stop phrase,
  immediately stop working, run the stop command below, and confirm.
- Keep responses and tool results compact. If the context is tight, finish with
  a state summary and let the driver compact/retry; do not burn the budget by
  re-reading the whole history.
- Use the tools and workspace normally; the loop does not change sandbox policy
  by itself.

## Stopping

A loop also stops automatically when its thread is stopped from the Codex UI,
archived, or deleted.

Run the driver stop command immediately whenever the user asks:

```sh
SCRIPT=$(find ~/.codex/plugins -path '*/codex-loop-agent/scripts/loop-agent.py' -print -quit)
python3 "$SCRIPT" stop --dir "$PWD"
```

The driver also watches for `/stop`, 停止无尽模式, 停止无限循环, 退出无尽模式,
停止循环, and similar phrases in real user messages and stops itself at the
next loop boundary.

## Configuration

Global state lives in `~/.codex/.codex-loop-agent.json`:

```json
{
  "disabled": false,
  "continuation": "继续，并深度检查暗病，遇到暗病和缺陷就修复"
}
```

- `disabled: true` immediately stops new rounds for every loop.
- `continuation` supports `{{lastAnswer}}`, `{{round}}`, and `{{task}}`
  placeholders.
- Per-session overrides are stored by the driver under
  `~/.codex/.codex-loop-agent/<session-id>.json`.

Useful commands:

```sh
python3 "$SCRIPT" config show
python3 "$SCRIPT" config set continuation "继续，遇到新问题就直接修复"
python3 "$SCRIPT" config disable
python3 "$SCRIPT" status --json
python3 "$SCRIPT" logs --dir "$PWD"
```

## Warnings

An endless loop spends tokens until it is stopped. Use `--max-rounds`,
`--until`, or the stop command to bound it. The driver uses only the flags
accepted by the current `codex exec resume` implementation. The driver is
designed for the same-session model: the app and the driver both append to the
same session log, so avoid manually stopping or archiving during an active
resume.
