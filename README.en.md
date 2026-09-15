# codex-loop-agent

An **Endless Loop plugin for the Codex desktop app**. Its usage model follows
[dsh-loop-agent](https://github.com/tyza66/dsh-loop-agent):

- Type `/forever` in any session to activate endless mode.
- After every completed answer, the loop driver injects a configurable
  continuation prompt into the **same session** and keeps the original task
  going.
- Continuations appear in the chat transcript as **ordinary user messages** and
  never block the chat window.
- Real user messages always win: messages you type yourself, and anything
  already waiting in the desktop queue, run first. Only when that queue is empty
  does the loop send the next continuation.
- The queue holds **at most one** of the loop's own continuations, so they never
  pile up into a wall of duplicate messages.
- Every kind of failure (spawn errors, timeouts, non-zero exits, model errors,
  context pressure, process crashes, and so on) is retried with exponential
  backoff. The loop **never exits on its own because of an error**; only an
  explicit user stop ends it.
- `/stop`, stop phrases such as `停止无尽模式`, the desktop stop button,
  archiving or deleting the session, and turning off the global switch all stop
  that session's loop.

中文版: [README.md](README.md)

## Install

Install as a Codex plugin:

```sh
codex plugin marketplace add https://github.com/tyza66/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

For local development, add a local path instead:

```sh
codex plugin marketplace add /path/to/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

After installing, open a new session and type `/forever`. Upgrades require a
reinstall, because the plugin cache is keyed by version.

## Quick start

Inside a Codex session, just say:

```text
/forever
```

The model locates and invokes the bundled driver script automatically. You can
also drive it from the command line:

```sh
SCRIPT="$HOME/.codex/plugins/cache/codex-loop-agent/codex-loop-agent/<version>/scripts/loop-agent.py"

# Show the loop status for every session
python3 "$SCRIPT" status --all

# Start an endless loop for the session in the current workspace
python3 "$SCRIPT" start --dir "$PWD"

# Stop the loop for the current session
python3 "$SCRIPT" stop --dir "$PWD"

# Change the default continuation prompt
python3 "$SCRIPT" config set continuation "Keep going and fix anything you find"
```

## How it works

The plugin ships a background driver, `scripts/loop-agent.py`. It does not
occupy the chat window; all of its work goes through Codex's own message queue:

1. On `start` it resolves the current Codex session: by default the newest
   session whose `turn_context.cwd` matches `--dir`, or an explicit
   `--session <uuid>`.
2. The driver polls the session JSONL record. If the newest entry is a real user
   message whose turn has not finished yet, it waits instead of running ahead.
3. Once the previous turn is complete, it delivers the continuation with
   `codex queue`, which is why it shows up in the desktop app as a normal user
   message.
4. A round only counts when the driver has **observed proof of consumption**:
   either the queued item was taken by the desktop, or the continuation showed
   up in the transcript. Otherwise it backs off and retries, so rounds are never
   invented.
5. Whenever user messages are queued, the driver yields and waits. It resumes
   sending continuations only after that queue is empty.
6. Injection failures, process errors, timeouts, non-zero exits, and context
   pressure are all retried, with a 32-second backoff ceiling by default.
7. Stop conditions: `/stop`, stop phrases, the `stop` command, `--max-rounds`,
   `--until`, the global switch being off, and the session being archived or
   deleted.

## Layout

```text
README.md                          # Chinese docs (default)
README.en.md                       # English docs
plugins/codex-loop-agent/
  .codex-plugin/plugin.json        # plugin manifest
  skills/forever/SKILL.md          # /forever activation and behavior contract
  scripts/loop-agent.py            # the endless loop driver
  test/test_loop_agent.py          # unit tests
```

## Commands

```sh
python3 scripts/loop-agent.py start [--session <uuid>] [--dir <dir>] [options]
python3 scripts/loop-agent.py stop [--session <uuid> | --last | --all | --dir <dir>]
python3 scripts/loop-agent.py status [--all | --session <uuid> | --last | --dir <dir>] [--json]
python3 scripts/loop-agent.py config show | set <key> <value> | enable | disable
python3 scripts/loop-agent.py logs [--session <uuid> | --last | --dir <dir>] [--lines N]
```

Common `start` options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--continuation` | global config | Continuation template supporting `{{lastAnswer}}`, `{{round}}`, and `{{task}}` |
| `--max-rounds` | `0` | Maximum continuation rounds; `0` means unlimited |
| `--until` | none | ISO time or unix timestamp; stops automatically when reached |
| `--initial-backoff-ms` | `1000` | Wait before the first retry after a failure |
| `--max-backoff-ms` | `32000` | Backoff ceiling |
| `--backoff-factor` | `2.0` | Exponential backoff multiplier |
| `--poll-ms` | `2000` | User-priority poll interval |
| `--unobserved-timeout-seconds` | `90` | Withdraw and retry if no consumption evidence appears in time |
| `--timeout-seconds` | `0` | Per-round time limit; `0` means unlimited |
| `--transport` | `queue` | Delivery mode: `queue` (desktop queue, recommended) or `exec` (CLI resume) |

## Configuration

Global config file: `~/.codex/.codex-loop-agent.json`

```json
{
  "disabled": false,
  "continuation": "继续，并深度检查暗病，遇到暗病和缺陷就修复"
}
```

- `disabled: true` immediately stops every loop from starting new rounds.
- `continuation` supports the `{{lastAnswer}}`, `{{round}}`, and `{{task}}`
  placeholders.
- Per-session overrides live in `~/.codex/.codex-loop-agent/<session-id>.json`.
  That value comes from an explicit setting at start time and takes priority
  over the global config.
- Commands for changing configuration:

```sh
python3 scripts/loop-agent.py config show
python3 scripts/loop-agent.py config set continuation "Keep going and fix new issues"
python3 scripts/loop-agent.py config disable
```

## Stopping

Any of the following stops that session's loop and withdraws any continuation
that has not been consumed yet, so no ghost message is left behind:

- Sending `/stop`, `/forever stop`, `停止无尽模式`, `停止无限循环`,
  `停止循环`, or a similar stop phrase in the session.
- Pressing the stop button in the Codex desktop app.
- Archiving or deleting the session.
- Running `python3 scripts/loop-agent.py stop --dir "$PWD"` (or with
  `--session` / `--last` / `--all`).
- Running `config disable` to turn off the global switch.
- Reaching the `--max-rounds` or `--until` limit.

## FAQ

**Why do I still see one continuation in the queue?**

A continuation has to enter the session as a user message for the loop to
continue, so it briefly appears in the queue before the desktop consumes it. The
driver guarantees at most one, and it does not inject while user messages are
pending.

**What if a user message has exactly the same text as the continuation?**

Message ownership uses both a text hash and an injection time window. Text alone
would misclassify a user message that happens to match the continuation as the
loop's own message. That is fixed: same-text user messages now correctly take
priority.

**Does the loop survive restarting Codex?**

The driver is an independent background process, so restarting the Codex app
does not kill it. It keeps checking whether the session still exists, and stops
by itself if the session is archived or deleted. Use the `stop` command to end
it manually.

## Development

```sh
# Run the unit tests
python3 -m unittest discover -s plugins/codex-loop-agent/test -p 'test_loop_agent.py'

# Validate the plugin manifest and structure
python3 "$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py" plugins/codex-loop-agent
```

After changing the driver, bump both `.codex-plugin/plugin.json` and the
`--version` string in `scripts/loop-agent.py`, because the plugin cache is keyed
by version.

## Warning

Endless mode keeps spending tokens until it is stopped. Use `--max-rounds`,
`--until`, or the `stop` command when you need a bound.

## License

MIT
