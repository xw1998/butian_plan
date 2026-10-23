---
name: butian-workflow
description: 正确使用补天 Butian MCP 完成任务执行、经验召回、证据登记、交付审核与梦境自省。用户提到补天、butian、数字人任务、经验教训、做梦、实验登记，或遇到 external_execution_required、admin_authorization_required、handoff_task_not_owned_or_active 等错误时使用。避免只登记任务未执行、错用证据路径、伪造审核、跨身份与做梦计数误读。
---

# 补天工作流

## 最快路径（先看这里）

让模型少走弯路的三步，取代逐个拼接身份、会话与子任务：

1. `butian_connect`：绑定当前成员一次。本机默认不需要口令；多个成员时必须显式指定，不会替你猜。之后所有调用自动继承身份，不必重复传 `actor_member_id`/`token`。
2. `butian_mode`：`{"action":"get"}` 看当前模式，`{"action":"set","mode":"team"}` 切换。**不需要 session_id**，只影响后续目标；运行中任务保留不取消。
3. `butian_context`：一次拿到当前成员、模式、可见任务及其 revision、以及下一步合法动作。用它代替自己维护 session_id 与 revision。

执行结果没被执行器确认时（`status` 为 `unknown`/`provider_lost`/`failed`），结果里带 `trustworthy: false` 与 `diagnosis.reason`：**不要把它当完成去提交审核，也不要手工改状态绕过确认**；先按 `next_actions` 调 `butian_context` 看合法下一步，再 `butian_provider_start` 用新会话重发（不要复用旧 `provider_session_id`）。

提交目标时 `mode` 可省略，默认用 `butian_mode` 当前值。重复提交同一句目标不会失败，但返回里会带 `duplicate_of`（已有 `task_id` 与状态）和 `duplicate_note`：先判断是不是同一件事，是就接着用旧任务，真要做并行任务就改一句目标描述。团队模式省略 `subtasks` 会按成员职责生成**确定性分工**（`plan_source=deterministic_role_split`，未调用模型）；要更细的拆解就显式传 `subtasks`，此时 `plan_source=host`。省略 `member_ids` 会把全部其他成员都排进分工（`worker_selection=all_other_members`），只想少数人参与时显式指定。**单人模式传 `member_ids` 会报 `member_ids_require_team_mode`，先切模式再提交。**

`butian_context` 的 `next_actions` 就是可复制的调用参数：`todo→ready`、`ready→认领`、`claimed→running`、`running→review` 四步直接给 `task_id`/`expected_revision`/`target`/`operation_key`，照抄即可，不必自己算 operation_key 或猜 revision；有可推进任务时它不会再劝你新建目标。派发者只拿到 `butian_task_tree`（别人名下的任务不该你代做），审核建议只给非本人任务。

工具第一页固定为「状态 / 连接 / 上下文 / 模式 / 提交目标 / 任务列表 / 目录 / schema」，不是全部工具；找工具用 `butian_tools_catalog`（支持 `query` 子串过滤名称与描述，无匹配返回 `catalog_query_no_match`），再用 `butian_tool_schema` 取单个 schema。`butian_task_claim` 的 `member_id` 默认取已连接成员，`butian_task_transition` 的 `operation_key` 缺省派生，`actor_member_id`/`token` 不是这些工具的必填项。

说了“我是谁”就不用再说：`butian_connect` 建立连接后，`member_id`/`actor_member_id`/`token` 由连接注入（schema 里仍声明必填，因为脱开连接它们确实必需）。没连接就调用成员类工具，会直接得到“先 `butian_connect`”的下一步。只有 `butian_hypothesis_action_grant`、`butian_tool_security_review`、`butian_tool_repair_confirm`、`butian_dispatcher_set`、`butian_host_authorization_*` 不会被注入——那里 `member_id` 指的是别人或独立角色，默认成自己会把授权与独立复核变成自我授权。可选参数传 `null` 视为未提供。

提交目标时问题模型字段可以直接写顶层（如 `acceptance_criteria`），与写在 `problem` 里等价；两处取值不同报 `problem_field_conflict`，**拼错或未知的参数报 `unknown_argument` 并列出可接受的参数名**——不会被静默忽略。验收标准为空会让任务停在 `needs_clarification`，此时按 `clarification.next_actions` 调 `butian_problem_update`（`expected_revision` 是问题定义 revision，不是任务 revision）。

报错统一带下一步：对象不存在（`knowledge_source_not_found`、`source_snapshot_not_found`、`memory_not_found`、`tool_version_not_found` 等）会指向能列出/建立该对象的工具；`butian_knowledge_source_add` 只登记哈希，正文要先 `butian_knowledge_source_verify`（哈希一致即落可读快照）或 `butian_knowledge_source_capture` 才能读；`butian_task_tree` 的 `payload` 已是解析后的对象。

这些便利不放宽边界：跨成员访问仍返回 `member_memory_access_denied`；共享部署可设 `BUTIAN_MCP_TOKEN_REQUIRED=1` 恢复逐次口令校验；启动成功不等于执行成功，也不等于独立审核完成。

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
- 查询最新 revision → transition todo→ready（`operation_key` 可省略，服务端按 `task:revision:target` 派生；显式传时须与操作内容一一对应）→ claim 给获授权执行者（`member_id` 可省略，默认取已连接成员）→ 查询新 revision → transition claimed→running。
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
