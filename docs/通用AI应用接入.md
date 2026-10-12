# 通用 AI 应用接入

补天是任务、证据、审核、记忆与工具资产运行时。宿主 AI 使用自己的模型，不要求 Ollama。支持 stdio MCP 的客户端都可按以下协议接入；客户端逐一兼容性尚未全量实测。

## 1. 安装与数据目录

在仓库目录执行 `python -m pip install -e .`，使用同一个 Python 解释器运行 `python -m butian.cli init`。可通过 `BUTIAN_HOME` 设置独立数据目录，避免多个项目共享身份与记忆。

客户端 MCP 配置通常包含以下字段，具体字段名称以客户端为准：

```json
{
  "mcpServers": {
    "butian": {
      "command": "python",
      "args": ["-m", "butian.cli", "mcp"],
      "env": {"BUTIAN_HOME": "D:/butian-data"}
    }
  }
}
```

若未安装项目，需将工作目录设为仓库目录；若客户端不支持工作目录，请先安装项目并填写解释器绝对路径。标准输出只用于 JSON-RPC，客户端先 initialize，再 notifications/initialized，再 tools/list。

## 2. 身份认证

成员身份在本地工作台或初始化管理流程中配置。调用受保护工具要携带 `member_id`、`actor_member_id` 和该成员的 `token`，不能通过修改 actor 字段冒充另一个成员。令牌应由本地管理流程设置并保存在客户端的安全配置中，不写入仓库、发布报告或聊天记录。

首次部署没有成员令牌时，可由可信本地管理员通过 Store.set_member_token 配置；MCP 不提供免认证的远程自助开户或令牌重置入口。团队派活者的资源权限不等于可读取其他成员全部私有记忆。

## 3. 宿主 AI 执行流程（无需模型 Provider）

1. tools/list 发现能力，使用已认证成员创建 solo/team session。
2. butian_task_create 创建任务；butian_task_list 读取当前 revision。butian_task_transition 转 ready，butian_task_claim 领取，再使用最新 revision 转 running。
3. butian_context_compose 装配人格、约束、任务与候选经验；宿主 AI 负责阅读并完成工作。候选经验只供参考。
4. 需要用户决策时调用 butian_user_question，收到答复后 butian_user_answer；重新领取、读取最新 revision 后继续。
5. 将结果保存为宿主与补天服务都能访问的文件，butian_evidence_record 登记 SHA-256 证据。
6. 转 review，读取当前 revision，butian_task_handoff 提交 summary、非空 evidence_ids、next_action 和 confidence。handoff 必须由当前任务受派成员提交，审核版本变化后需重新提交。
7. butian_task_review 批准或返工。批准仍检查当前交付和证据，不能靠状态字段跳过。审核流程会记账候选经验，后续通过 recall/practice/outcome 记录使用和反证。
8. 关闭重启后继续使用同一 BUTIAN_HOME，任务、证据和独立成员记忆保留。

这些操作由宿主 AI 自己编排。补天服务不会在后台自动继承宿主模型调用权限，也不会把未执行工作标为成功。

## 4. 补天主动驱动子 Agent（可选）

只有需要补天主动启动独立执行端时才配置 providers.json。可选 local-command、HTTP/OpenAI-compatible、MCP、Ollama、manual，不要求同时配置。

MCP Provider 配置示例：

```json
{
  "providers": [{
    "type": "mcp",
    "command": ["python", "path/to/your_agent_server.py"],
    "timeout": 30,
    "context_window": 32768
  }]
}
```

context_window 示例值不能直接照抄，必须填写执行端确认支持的窗口；未知时不启动自动任务。执行端需暴露可无歧义选择、具有字符串任务参数的 Agent 工具，其他必填参数不满足会如实失败。此桥接是单进程无状态子会话，未宣称支持远端取消、持续用户回复或模型切换。超时会停止桥接进程并返回 unknown；不能由此推断远端外部副作用已撤销。

## 5. 兼容与验收范围

已验证 Windows Python 独立 stdio 宿主从握手到证据审核 done，以及重启后的任务与记忆读取；未配置模型 Provider、没有使用 Ollama。另有真实 MCP Agent 工具子进程调用与无响应超时回收测试。

这证明协议与业务运行时链路可用，不证明每个客户端都已适配，也不把受控测量数据称为模型自主学习。只有 API/进程接口的应用使用对应适配器；没有可调用接口的应用保留人工交接路径。
