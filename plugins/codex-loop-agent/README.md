# codex-loop-agent 插件

Codex 无尽模式插件：把 dsh-loop-agent 的“按需无尽循环”带到 Codex 的同一个会话里。

## 工作原理

插件自带一个后台驱动脚本 `scripts/loop-agent.py`：

1. `start` 时解析出当前 Codex 会话（按 `--dir` 匹配最新的 `turn_context.cwd`，或显式传 `--session`）。
2. 驱动轮询会话 JSONL：如果最近一条消息是真实用户消息且还没有对应的 `task_complete`，就等待该轮跑完，绝不抢跑。
3. 上一轮完成后，用 `codex exec resume <session> <continuation>` 把延续语注入同一个会话。
4. 任何错误都按指数退避原样重试，包括启动失败、超时、非 0 退出码、模型报错、上下文过长和进程异常；循环不会因错误自动结束。
5. 直到用户 `/stop`、发送停止短语、运行 `stop` 命令、`--max-rounds` 或 `--until` 到期，或全局开关关闭。
6. 对应会话被归档、删除，或用户在 Codex UI 中显式停止该任务时，该会话的循环也会停止。

## 目录

```text
.codex-plugin/plugin.json   # 插件清单
skills/forever/SKILL.md     # /forever 激活与循环行为约定
scripts/loop-agent.py       # 无尽循环驱动（start/stop/status/config/logs）
test/test_loop_agent.py     # 单元测试
```

## 命令

```sh
python3 scripts/loop-agent.py start [--session <uuid>] [--dir <dir>] [options]
python3 scripts/loop-agent.py stop [--session <uuid> | --last | --all | --dir <dir>]
python3 scripts/loop-agent.py status [--all | --session <uuid> | --dir <dir>] [--json]
python3 scripts/loop-agent.py config show | set <key> <value> | enable | disable
python3 scripts/loop-agent.py logs [--session <uuid> | --last | --dir <dir>] [--lines N]
```

`start` 常用选项：

| 选项 | 默认 | 含义 |
| --- | --- | --- |
| `--continuation` | 全局配置 | 延续语模板，支持 `{{lastAnswer}}`、`{{round}}`、`{{task}}`；也可用 `config set continuation <文本>` 持久化 |
| `--max-rounds` | `0` | 最多执行的延续轮数，`0` 为不限 |
| `--until` | 无 | ISO 时间或 unix 时间戳，到期自动停 |
| `--initial-backoff-ms` | `1000` | 首次失败重试等待 |
| `--max-backoff-ms` | `32000` | 退避上限 |
| `--backoff-factor` | `2.0` | 指数退避乘数 |
| `--poll-ms` | `2000` | 用户消息优先检查间隔 |

## 测试

```sh
python3 -m unittest discover -s test -p 'test_*.py'
```

## 许可证

MIT
