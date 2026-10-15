---
name: butian-workflow
description: 正确使用补天 Butian MCP 完成任务执行、经验召回、证据登记、交付审核与梦境自省。用户提到补天、butian、数字人任务、经验教训、做梦、实验登记，或遇到 external_execution_required、admin_authorization_required、handoff_task_not_owned_or_active 等错误时使用。避免只登记任务未执行、错用证据路径、伪造审核、跨身份与做梦计数误读。
---

# 补天工作流

## 先确认连接与权限

1. 使用宿主提供的连接器发现功能确认实际连接；不要硬编码某个用户的连接 ID、工作目录、成员或凭据。
2. 调用 `butian_status`。注意：MCP 可用不代表 HTTP 网页已更新，也不代表正在使用同一个数据目录。
3. 优先调用 `butian_tools_catalog`（limit 10～20、按名称 prefix 筛选）和 `butian_tool_schema(name)`，按实际 schema 构造请求。旧版无查询工具时使用宿主支持的工具发现，不用缺参数调用试探接口。
4. 使用已授权身份。桥接器可能内部注入 token，以实际暴露 schema 为准；不要索取、打印或写入秘密，不要冒充 reviewer/admin，不要读取桥接器凭据。
5. 先查询已有任务，保留 task_id、session_id、revision、assignee、状态与验收条件；不要为消除错误偷偷新建重复任务。

## 执行两条合法路径

### 标准 AI 宿主执行

- 新任务用 `butian_goal_submit` 登记目标、约束及真实验收条件。
- 执行前按成员召回经验，保留 recall_id。`no_match` 正常，不编造经验。
- 用 `butian_execution_export` 获取绑定任务与问题 revision 的上下文；符合条件的 host/manual 任务会自动推进 running，不要额外重复 claim。
- 在授权范围内完成实际工作；保存真实命令、输入、输出、局限与负证据。
- 先用 `butian_evidence_record(path)` 登记文件，再用 evidence_id 验证和引用。
- 用导出包中的 task_revision/problem_revision 和唯一 submission_id 调用 `butian_execution_submit`；如实填写 work_done/work_not_done，确认返回 review。
- 按真实审核策略提交给获授权审核者。没有独立审核就明确未独立审核，不靠切换姓名制造独立性，不把 review 写成 done。

### 已有普通任务 / 手动交付

- `butian_task_create` 仅登记 todo，不等于执行成功。
- 查询最新 revision → transition todo→ready（唯一 operation_key）→ claim 给获授权执行者 → 查询新 revision → transition claimed→running。
- 实际执行后登记证据，按 `butian_task_handoff` 的实际 schema 交付；再按合法状态与审核策略处理 review。handoff 不一定自动改变任务状态，必须查询返回及最新状态。
- 不直接改数据库、绕过状态机、覆盖归属或强行把终态任务恢复 running。

## 遇错恢复

- 优先读取 MCP 工具错误正文 `details.current`、`missing_conditions`、`required`、`next_actions`；动作片段可能缺认证、操作键，先查询 schema 补齐。
- `external_execution_required` 是执行模式不匹配，不仅是状态未 running。已有任务不要自行改 payload/provider；确认采用宿主还是配置 Provider 路径，必要时请求任务拥有者决策。
- `admin_authorization_required`：当前成员 MCP 实验入口不可用；如实记录阻塞，交获授权操作者处理，不换身份试权限，不声称实验已登记。
- `handoff_task_not_owned_or_active`：核对身份、assignee、running/review 状态及 revision；跨成员无权限就停止，不冒用身份。
- `evidence_not_found`：引用已登记 evidence_id，不传文件路径作 evidence_ref。
- revision 冲突先读最新状态，不盲重试带副作用的旧请求；幂等键只能用于同一操作内容。

## 经验与做梦

- 反馈真实召回使用结果；有用反馈、命中或文件哈希不证明避免错误或结论成立。
- 仅对获授权成员调用 `butian_dream_run`，不要遍历全员为单人请求补做梦。默认尊重门控，不循环降低 min_fragments/min_interval 刷数。
- `completed` 才是自省完成；`skipped` 是门控检查未执行。后台维护会检查所有成员，所以全员跳过记录增长不代表全员做梦，也不证明记忆串库。
- 自省候选与假设未经验证；真实验证动作需显式授权、独立证据，不能用梦境内容自证成功。
- 团队看板为全员累计；逐成员表按成员库统计；进化页最近记录不是累计总数。

## 收尾核对

逐项报告：实际完成工作、证据 ID 与核验、实验登记是否成功、任务当前状态、交付是否提交、审核身份与是否独立、未完成项。工具成功只证明对应步骤，不替代科学验收；权限或状态阻塞必须明示。没有授权不要提交、推送、发布、删除或修改其他成员资料。
