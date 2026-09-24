# Personal KB v2：单入口可复用记忆系统设计

日期：2026-09-24
状态：架构决策草案，先文档化，分阶段实现

## 目标

把需求、bug、设计取舍、运行时检索结果、验证结果和未完成状态放进同一个可追踪生命周期，使下一个 AI 会话能通过一个入口恢复和复用：

```text
发生/检索/验证
      ↓
 kb memory ingest                 # 唯一写入入口
      ↓
 event + candidate + evidence     # 全部保留，可信度分层
      ↓
 dedupe / entity / relation
      ↓
 verify / promote / supersede
      ↓
 kb memory recall                 # 混合检索和关系扩展
      ↓
 adopt / reject / correct         # 反馈回系统
```

“全部记忆下来”在本设计中表示：原始事件可追踪保存；只有经过证据门禁的内容才能作为长期事实参与默认回答。

## 当前系统的真实起点

已有能力：

- 一个物理数据根，`records`、`retained-files`、`manifests`、`runtime`、`cache` 分层。
- JSONL 长期记录，六类 kind，证据指针和 optimistic revision。
- 倒排索引与时间索引；运行时 closeout、adoption、challenge、Cursor 审计。
- 当前代码、运行态、日志、测试优先于历史命中。

关键缺口：

- `remember/update` 是人工选择的写入动作，没有统一 ingest 事件层；runtime 命中、用户需求、bug 观察的入口不同。
- 没有 canonical entity/object 与关系边；`aliases/tags/trigger_terms` 不能表达“此 bug 由此配置导致”或“这条需求替代旧需求”。
- 当前默认没有向量 embedding；关键词相关不等于语义相关，跨说法复用有限。
- 没有统一的 `valid_from/valid_until` 与冲突集合；stale top-3 高时只能在检索后人工筛选。
- Cursor/Codex 的 session/topic/closeout 关联才刚开始补齐，运行时事实尚未自动沉淀为候选记忆。
- 没有宿主启动恢复块；记忆存在，但新会话是否读到仍依赖 Skill/模型行为。

## 统一对象模型

先保留 JSONL 和单根，增加四个逻辑对象（不新建物理库）：

### 1. `memory_event`：发生过什么

保存用户消息中的需求、工具返回、检索命中、验证命令、失败和采用反馈。事件是 append-only，可重放、可审计，但默认不直接作为事实回答。

```json
{
  "event_id": "evt_...",
  "event_type": "user_requirement|runtime_observation|retrieval|verification|adoption|rejection",
  "session_id": "...",
  "topic_id": "...",
  "source": {"client": "cursor", "turn_id": "...", "tool": "Shell"},
  "payload_ref": "retained://...",
  "occurred_at": "2026-09-24T...Z",
  "evidence": [{"type": "command", "value": "pytest ...", "exit_code": 0}],
  "status": "observed|verified|rejected"
}
```

### 2. `memory_object`：可以复用什么

把现有六种 kind 扩展为逻辑类型，而不是新增多个物理库：

- `requirement`：用户明确要求或约束
- `issue`：已复现问题/根因
- `decision`：已确认取舍
- `experience`：验证过的做法
- `map`：路径、服务、资源映射
- `procedure`：可执行步骤/命令
- `state`：当前阶段、阻塞、下一步
- `episode`：一次完整任务或运行过程的摘要

每个对象都要有：`object_id, kind, title, summary, scope, status, confidence, valid_from, valid_until, evidence_refs, source_event_ids, embedding_ref`。

### 3. `memory_edge`：对象之间怎么关联

第一版只允许六类边，避免 LLM 任意造关系：

```text
supports       evidence/event → object
contradicts    object → object
supersedes     newer object → older object
derived_from   object → event/object
applies_to     object → repo/module/service/entity
caused_by      issue → config/change/event
```

### 4. `entity`：稳定对象是什么

对 repo、模块、服务、设备、用户偏好、接口、命令和业务概念建立 canonical ID。名称变化只更新 alias，不新建事实。实体解析必须可回溯，不能仅靠 embedding 相似度合并。

## 单一入口

保持兼容的 `kb.py`，新增统一子命令，不让 Cursor、Codex、脚本各自拼接写入格式：

```bash
kb.py memory ingest --event-json <file> [--session-id S] [--topic-id T]
kb.py memory recall "query" [--scope repo:files] [--as-of 2026-09-24]
kb.py memory link OBJECT --relation supports --target EVENT
kb.py memory verify OBJECT --evidence-command "pytest ..."
kb.py memory promote OBJECT --reason "user-confirmed|runtime-verified"
kb.py memory supersede OLD --by NEW --reason "current evidence changed"
kb.py memory bootstrap [--quiet]
```

旧的 `retrieve/remember/update/closeout` 继续工作，内部转成上述事件，逐步迁移，不做一次性破坏性改名。

## 混合检索流程

```text
query
 ├─ intent/scope parser: repo, module, entity, time, kind, as-of
 ├─ lexical: exact title/alias/trigger/path/symbol
 ├─ vector: summary + verified evidence embedding
 ├─ graph: entity neighbors, supports, supersedes, caused_by
 ├─ temporal/conflict gate: valid_at, status, current branch, dirty evidence
 ├─ rerank: evidence > scope > freshness > relation > lexical/vector score
 └─ answer context: object + why matched + evidence + conflicts + validity
```

建议的第一版分数只用于排序，不用于放宽门禁：

```text
final = 0.30 lexical + 0.25 vector + 0.20 graph
      + 0.15 evidence + 0.10 freshness
```

`contradicts`、`superseded`、`dirty_worktree`、`unverified` 默认降权或阻断；用户指定 repo/branch 和当前运行证据可覆盖历史排序。

## 维护循环（借鉴 supergoal 的确定性边界）

### Formation：写入

- 每次 user requirement、runtime observation、retrieval result、verification result 都进入 event journal。
- 只保存引用和脱敏摘要；完整原文按现有 retained-files 规则保存。
- dedupe 只合并相同 scope/kind 的近重复对象，保留来源事件。

### Recall：使用

- 启动/恢复/压缩时由 adapter 调用 `memory bootstrap`，输出当前项目的 goal、阻塞、最近验证 lesson、待复核事实。
- 每次 recall 返回“命中原因”和证据，不只返回文本片段。
- 使用后写 adoption/rejection event；拒绝原因成为后续排序反馈。

### Maintenance：巩固

- 定期或 closeout 后运行：实体解析、关系提取、冲突检测、过期扫描、embedding 更新、图索引重建。
- 只有存在验证事件才能从 `candidate` 变为 `verified`。
- 新对象 supersede 旧对象，不删除旧证据；`valid_until` 标记旧事实失效。
- 失败或 dirty evidence 进入 `pending_recheck`，不能重复 heat。

## 分阶段实现

### P0：统一记忆事件和恢复（最先做）

- 新增 event schema、`memory ingest`、`memory bootstrap`。
- 让 retrieve/closeout/remember/update 自动产出事件；补 session/topic/turn/source。
- Cursor/Codex adapter 在启动和压缩后调用 bootstrap。
- 验收：需求、runtime 命中、验证、拒绝都能从一个 JSONL 事件流重建；事件丢失率为 0；旧 CLI 测试全过。

### P1：对象、证据、时间和冲突

- 把现有 record 映射为 memory_object；新增 `status/confidence/valid_* /source_event_ids`。
- 加 `supersedes/contradicts/supports` 边文件或同根 JSONL。
- 验收：同一需求更新后旧记录不会再排在当前事实前；冲突命中必须同时展示。

### P2：向量作为第二召回通道

- 先做可替换 embedding adapter，默认本地模型；embedding 是 cache，不是事实源。
- 记录 `embedding_model/version/dim/input_hash`，模型变更可重建。
- hybrid evaluator 用固定任务集测 recall、precision、stale exposure、adoption，不只测 Recall@K。
- 验收：语义改写能召回；stale top-3 和 map noise 下降；无 embedding 时 lexical 路径仍可用。

### P3：关系图和受限本体

- 用 JSONL adjacency 实现六类边；对 `Memory, Entity, Event, State, Decision, Constraint, Action, Evidence, Outcome` 做 schema 校验。
- 先支持一跳/两跳扩展和关系解释；数据量、查询延迟和并发达到门槛后再评估 SQLite/SQLite-vec、Qdrant 或图数据库。
- 验收：同一 bug 能通过“问题 → caused_by → 配置 → applies_to → 服务”解释；关系不存在时不编造。

### P4：自动巩固和质量闭环

- closeout 后异步候选提取；人工/主 Agent 只审核候选。
- contradiction、stale、duplicate、pending_recheck 进入维护队列。
- 按 topic 统计检索成本、采用、决策影响和重复劳动。
- 验收：可复用结论的二次任务减少重复检索/验证；每条 promotion 有证据；可从事件重建对象和索引。

## 暂不做

- 不立即引入 Neo4j、MongoDB、Qdrant、Redis 四件套。
- 不用 LLM 自动把所有聊天升级为 verified。
- 不把向量相似度当事实判断。
- 不删除旧记录来“解决”冲突。
- 不把本体做成学术 OWL 工程；先用小而稳定的业务概念和关系。

## 成功指标

每个窗口固定报告：

- `event_capture_rate`：应记录事件中真正进入 event journal 的比例。
- `verified_promotion_rate`：有证据升级的比例。
- `recall_useful_rate`：命中后实际改变决策/修复/写入的比例。
- `stale_exposure_rate`、`contradiction_exposure_rate`、`map_noise_rate`。
- `duplicate_retrieval_rate`、`missing_closeout_rate`、`pending_recheck_repeat_rate`。
- 同类任务的 transcript token、重复验证次数、完成时间和错误套用次数。

目标先定为可观测，不先定漂亮数字；连续两个窗口有基线后再设阈值。
