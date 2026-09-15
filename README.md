# codex-loop-agent

给 Codex 桌面版做的**无尽模式（Endless Loop）插件**。使用形式参照
[dsh-loop-agent](https://github.com/tyza66/dsh-loop-agent)：

- 在任意会话里输入 `/forever` 激活无尽模式；
- 每轮回答结束后，循环驱动会在**同一个会话**里自动注入一段可配置的“延续语”，继续原来的任务；
- 延续语以**普通用户消息**的形式出现在聊天记录里，不阻塞聊天窗口；
- 真实用户消息始终优先：你手动发的消息、以及你在桌面端排队里的消息，都会先跑完，之后才继续发延续语；
- 队列里最多保留 **1 条**循环自己的延续语，不会堆积成一串重复消息；
- 任何层面的错误（启动失败、超时、非 0 退出码、模型报错、上下文过长、进程异常……）都按指数退避重试，**永不因错误自动退出**，只有用户显式停止才会结束；
- `/stop`、`停止无尽模式` 等停止短语、停止按钮、归档或删除会话、全局开关关闭，都会停止该会话的循环。

English version: [README.en.md](README.en.md)

## 安装

作为 Codex 插件安装：

```sh
codex plugin marketplace add https://github.com/tyza66/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

本地开发时直接加本地路径：

```sh
codex plugin marketplace add /path/to/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

安装后开启一个新会话，输入 `/forever` 即可使用。升级版本后需要重新安装，插件缓存以版本号为键。

## 快速开始

在 Codex 会话里直接说：

```text
/forever
```

模型会自动定位并调用插件内置的驱动脚本。手动操作也可以用命令行：

```sh
SCRIPT="$HOME/.codex/plugins/cache/codex-loop-agent/codex-loop-agent/<版本号>/scripts/loop-agent.py"

# 查看所有会话的循环状态
python3 "$SCRIPT" status --all

# 在当前工作区启动当前会话的无尽循环
python3 "$SCRIPT" start --dir "$PWD"

# 停止当前会话
python3 "$SCRIPT" stop --dir "$PWD"

# 设置默认延续语
python3 "$SCRIPT" config set continuation "继续，遇到暗病就修复"
```

## 工作原理

插件自带一个后台驱动脚本 `scripts/loop-agent.py`，它不占用聊天窗口，全部工作通过 Codex 自己的消息队列完成：

1. `start` 时解析出当前 Codex 会话：默认按 `--dir` 匹配最新的 `turn_context.cwd`，也可以显式传 `--session <uuid>`。
2. 驱动轮询会话 JSONL 记录。如果最新一条是真实用户消息、且对应的回合还没结束，就等待它跑完，绝不抢跑。
3. 上一轮完成后，通过 `codex queue` 把延续语投递到同一个会话，所以在桌面端看起来就是一条普通用户消息。
4. 投递后必须**亲眼观察到消费证据**（队列项被桌面端取走，或延续语出现在会话记录里）才记为完成一轮；否则按超时退避重试，不会凭空累加轮次。
5. 只要有用户消息正在排队，驱动就礼让并等待；队列清空后才继续发延续语。
6. 注入失败、进程异常、超时、非 0 退出、上下文压力等任何错误都会重试，退避上限默认 32 秒。
7. 停止条件：`/stop`、停止短语、`stop` 命令、`--max-rounds`、`--until`、全局开关关闭，以及会话被归档或删除。

## 目录结构

```text
README.md                          # 中文说明（默认）
README.en.md                       # 英文说明
plugins/codex-loop-agent/
  .codex-plugin/plugin.json        # 插件清单
  skills/forever/SKILL.md          # /forever 激活与循环行为约定
  scripts/loop-agent.py            # 无尽循环驱动
  test/test_loop_agent.py          # 单元测试
```

## 命令

```sh
python3 scripts/loop-agent.py start [--session <uuid>] [--dir <dir>] [options]
python3 scripts/loop-agent.py stop [--session <uuid> | --last | --all | --dir <dir>]
python3 scripts/loop-agent.py status [--all | --session <uuid> | --last | --dir <dir>] [--json]
python3 scripts/loop-agent.py config show | set <key> <value> | enable | disable
python3 scripts/loop-agent.py logs [--session <uuid> | --last | --dir <dir>] [--lines N]
```

`start` 常用选项：

| 选项 | 默认 | 含义 |
| --- | --- | --- |
| `--continuation` | 全局配置 | 延续语模板，支持 `{{lastAnswer}}`、`{{round}}`、`{{task}}` |
| `--max-rounds` | `0` | 最多执行的延续轮数，`0` 表示不限 |
| `--until` | 无 | ISO 时间或 unix 时间戳，到期自动停止 |
| `--initial-backoff-ms` | `1000` | 首次失败后的重试等待 |
| `--max-backoff-ms` | `32000` | 退避上限 |
| `--backoff-factor` | `2.0` | 指数退避乘数 |
| `--poll-ms` | `2000` | 用户消息优先检查间隔 |
| `--unobserved-timeout-seconds` | `90` | 投递后未观察到消费证据的超时，超时撤回并重试 |
| `--timeout-seconds` | `0` | 单轮最大耗时，`0` 表示不限 |
| `--transport` | `queue` | 投递方式：`queue`（桌面端队列，推荐）或 `exec`（CLI resume） |

## 配置

全局配置文件：`~/.codex/.codex-loop-agent.json`

```json
{
  "disabled": false,
  "continuation": "继续，并深度检查暗病，遇到暗病和缺陷就修复"
}
```

- `disabled: true` 会立即停止所有循环继续开新轮次。
- `continuation` 支持 `{{lastAnswer}}`、`{{round}}`、`{{task}}` 占位符。
- 会话级覆盖保存在 `~/.codex/.codex-loop-agent/<session-id>.json`；该值来自启动时的显式设置，优先级高于全局配置。
- 修改配置的命令：

```sh
python3 scripts/loop-agent.py config show
python3 scripts/loop-agent.py config set continuation "继续，遇到新问题就直接修复"
python3 scripts/loop-agent.py config disable
```

## 停止方式

以下任一种都会停止对应会话的循环，并撤回尚未被消费的延续语，避免留下“幽灵消息”：

- 在会话里发送 `/stop`、`/forever stop`、`停止无尽模式`、`停止无限循环`、`停止循环` 等；
- 在 Codex 桌面端点停止按钮；
- 归档或删除该会话；
- 运行 `python3 scripts/loop-agent.py stop --dir "$PWD"`（或 `--session` / `--last` / `--all`）；
- 运行 `config disable` 关闭全局开关；
- 达到 `--max-rounds` 或 `--until` 限制。

## 常见问题

**为什么队列里还是会看到一条延续语？**

延续语本来就要作为用户消息进入会话才能让循环继续，因此它在被消费前会短暂出现在队列里。驱动保证最多只有一条，并且在用户有排队消息时不会注入。

**用户消息和延续语文本一模一样会怎样？**

驱动的消息归属判断同时使用文本哈希和注入时间窗口。只靠文本判断会把用户手输的同文本消息误认为循环自己的消息，现在已经修复，同文本的用户消息会正常优先执行。

**重启 Codex 后还在跑吗？**

驱动是独立后台进程，重启 Codex 应用不会自动杀掉它；但它会持续检测会话是否存在。如果会话被归档或删除，循环会自行停止。需要手动结束时用 `stop` 命令。

## 开发

```sh
# 运行单元测试
python3 -m unittest discover -s plugins/codex-loop-agent/test -p 'test_loop_agent.py'

# 校验插件清单与结构
python3 "$HOME/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py" plugins/codex-loop-agent
```

改动脚本后记得同步提升 `.codex-plugin/plugin.json` 与 `scripts/loop-agent.py --version` 的版本号，因为插件缓存以版本号为键。

## 注意

无尽模式会持续消耗 token，直到被停止。必要时用 `--max-rounds`、`--until` 或 `stop` 命令加以限制。

## 许可证

MIT
