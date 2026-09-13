# codex-loop-agent

Codex 无尽模式插件（Endless Loop）。使用形式参照
[dsh-loop-agent](https://github.com/tyza66/dsh-loop-agent)：

- 在任何会话里输入 `/forever` 激活无尽模式；
- 激活后，模型每次回答完都会由循环驱动在**同一个会话**里自动注入一段可配置的“延续语”，继续原来的任务；
- 真实用户消息始终优先；
- 失败按指数退避重试；上下文压力类错误会提示压缩后继续；
- 输入 `/stop`、`停止无尽模式` 等即可停止；也可以运行 `stop` 命令或关掉全局开关。

## 安装

把本仓库作为 Codex 插件市场安装：

```sh
codex plugin marketplace add https://github.com/tyza66/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

本地开发时也可以直接加本地路径：

```sh
codex plugin marketplace add /Volumes/Old_Solidity/Projects/tyza66/codex-loop-agent
codex plugin add codex-loop-agent@codex-loop-agent
```

安装后开始新线程，输入 `/forever` 即可使用。

## 激活与停止

```sh
# 查看状态
python3 plugins/codex-loop-agent/scripts/loop-agent.py status --all

# 打开全局开关（默认开）
python3 plugins/codex-loop-agent/scripts/loop-agent.py config enable

# 在当前工作区启动当前会话的无尽循环
python3 plugins/codex-loop-agent/scripts/loop-agent.py start --dir "$PWD"

# 停止当前会话
python3 plugins/codex-loop-agent/scripts/loop-agent.py stop --dir "$PWD"

# 设置默认延续语
python3 plugins/codex-loop-agent/scripts/loop-agent.py config set continuation "继续，遇到暗病就修复"
```

在 Codex 会话里直接说 `/forever` 激活、`/stop` 停止即可，模型会调用上面的脚本。

## 配置

全局状态文件：`~/.codex/.codex-loop-agent.json`

```json
{
  "disabled": false,
  "continuation": "继续，并深度检查暗病，遇到暗病和缺陷就修复"
}
```

延续语支持 `{{lastAnswer}}`、`{{round}}`、`{{task}}` 占位符。每个会话的运行时状态在
`~/.codex/.codex-loop-agent/<session-id>.json`。

更多说明见 [插件 README](plugins/codex-loop-agent/README.md)。

## 许可证

MIT
