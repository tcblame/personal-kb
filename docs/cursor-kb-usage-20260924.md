# Cursor Personal KB 使用审计（2026-09-18 至 2026-09-24）

本报告只保存聚合指标；原始 Cursor transcript 和 closeout 位于本机私有路径。日期按 transcript 文件 mtime 选取，文件内嵌旧 turn 不被假定为窗口内发生。

## 结果

- 主会话：16 个，其中 13 个含 KB 请求。
- 可识别的非 help 请求：128；retrieve 63、closeout 57、update 7、curate-sessions 1。
- help 请求：5，不纳入有效使用。
- 这些请求的执行状态：128 条为 unknown，因为 Cursor transcript 没有提供对应工具返回；不能算成功检索。
- 唯一 closeout 关联：28 条，来自 9 个主会话。
- 子 Agent：18 个被扫描，但没有纳入主会话使用率。

## 解释

Cursor 的确比 Codex 更常使用 Personal KB，且使用集中在 4d70aca5、64eb2053、29747c1b 等长会话；但 transcript 中“发出了 Shell 请求”与“命令执行成功”是两层证据。当前可严谨确认的是请求量和少量通过唯一 closeout_id 对上的 runtime adoption，不能把其余请求直接算作命中、采用或节省 token。

## 本轮迭代

- 新增 `scripts/kb_audit_cursor_sessions.py`：识别可执行 Shell 请求，隔离 help、heredoc 不确定项、主会话/子 Agent，并通过唯一 closeout_id 做保守关联。
- retrieve 输出增加 `session_id`、`topic_id`、`query_chars`；closeout 输出增加 `topic_id`，为按 topic 预算和会话归因提供稳定字段。
- SKILL 增加 Cursor 审计规则：unknown 不算成功，不能用命令文字、Skill 阅读或相邻时间推断 adoption。

## 后续验收

下一窗口应让 Cursor 调用显式传 `--session-id`、`--topic-id`，并在 closeout 保留 `linked_retrieval_ids`、`adoption_effects`、`written_entry_ids`。比较时同时看请求、返回、确认采用、写入和任务结果；缺失返回或缺失 session_id 单列为未知。
