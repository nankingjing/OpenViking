# Pi Coding Agent 扩展

当你希望 [Pi coding agent](https://pi.dev) 使用 OpenViking 作为长期记忆和上下文后端时，使用这个 OpenViking 扩展。它运行在 Pi 原生 TypeScript extension 系统内：不需要 MCP sidecar、不需要 wrapper 命令、不需要额外 daemon。

源码：[examples/pi-coding-agent-extension](https://github.com/volcengine/OpenViking/tree/main/examples/pi-coding-agent-extension)

## 能力

- **每次模型调用前自动召回**：用当前 prompt 搜索 OpenViking，并把相关记忆注入同一轮。
- **每次运行后自动捕获**：把用户 prompt、steering/follow-up 消息、assistant 文本和工具调用摘要同步到 OpenViking session。
- **默认启用上下文接管**：已提交的 Pi 历史会在模型视角中替换为 OpenViking archive overview，再保留少量最近 live turns。
- **手动工具**：注册 `viking_search`、`viking_read`、`viking_browse`、`viking_remember`、`viking_forget`、`viking_add_resource`、`viking_archive_expand`。
- **手动命令**：`/viking` 显示连接状态，并支持手动 commit。

上下文接管意味着长期上下文由 OpenViking 管理，而不是由 Pi 本地 compactor 管理。扩展不会重写 Pi session 文件；它通过 Pi 的 `context` hook 过滤模型实际看到的上下文。

## 安装

前置条件：

- Pi coding agent `0.80.3` 或兼容的 `0.80.x`
- Node.js 20+
- 一个可访问的 OpenViking 服务

把扩展复制到 Pi 全局扩展目录：

```bash
mkdir -p ~/.pi/agent/extensions
cp -r examples/pi-coding-agent-extension ~/.pi/agent/extensions/openviking
```

下次启动 Pi 时会自动发现 `~/.pi/agent/extensions/openviking/index.ts`。

## 配置

本地无鉴权服务可直接使用默认配置：

```bash
pi
```

远程服务可在启动 Pi 前设置环境变量：

```bash
export OPENVIKING_URL="https://your-openviking.example.com"
export OPENVIKING_API_KEY="<api-key>"
export OPENVIKING_ACCOUNT="my-team"   # 可选，多租户 account
export OPENVIKING_USER="alice"        # 可选，多租户 user
export OPENVIKING_AGENT_ID="pi"       # 可选，agent 身份
pi
```

也可以编辑 `~/.pi/agent/extensions/openviking/config.json`：

```json
{
  "enabled": true,
  "endpoint": "https://your-openviking.example.com",
  "apiKey": "<api-key>",
  "account": "my-team",
  "user": "alice",
  "agentId": "pi"
}
```

环境变量优先级高于 `config.json`。

## 上下文接管配置

扩展默认启用 takeover：

```json
{
  "takeover": {
    "enabled": true,
    "tokenThreshold": 30000,
    "keepRecentTurns": 3,
    "overviewBudget": 3000,
    "overviewPollMs": 2000,
    "overviewPollMax": 15
  }
}
```

当已同步 token 压力超过 `tokenThreshold` 时，扩展会 flush 队列、commit OpenViking session、短暂等待最新 archive overview、推进边界，并在模型上下文中只保留 `keepRecentTurns` 个 live turns。OpenViking 不可用时会 fail open，Pi 继续按原行为运行。

## 验证

启动 Pi 后观察 OpenViking 启动状态，然后输入：

```text
/viking
```

也可以让 Pi 记住一个事实，再从另一个 shell 验证：

```bash
ov ls viking://user/default/sessions/
ov find "你让 Pi 记住的事实"
```

## 开发

```bash
cd examples/pi-coding-agent-extension
npm install
npm run typecheck
npm test
```

Live acceptance test 会驱动真实 Pi、真实 OpenViking 服务和真实模型提供方：

```bash
OPENVIKING_URL=https://your-ov \
OPENVIKING_API_KEY=... \
SUPER_RELAY_API_KEY=... \
npm run e2e
```

## 参见

- [扩展 README](https://github.com/volcengine/OpenViking/blob/main/examples/pi-coding-agent-extension/README.md)
- [上下文接管设计](https://github.com/volcengine/OpenViking/blob/main/examples/pi-coding-agent-extension/TAKEOVER.md)
- [Agent 集成概览](./01-overview.md)
- [MCP 客户端](./06-mcp-clients.md)：通用工具型接入
