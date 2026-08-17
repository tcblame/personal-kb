# personal-kb

面向 Codex/Claude 的 RAG-first Personal KB Skill：把一个物理 KB 根变成可成长的“老员工”记忆层。默认只在跨会话历史依赖时检索；用户明确要求记住的内容，以及已验证、稳定、可复用的 bug/设计/资源经验，都可以沉淀。它不是为了复制所有聊天记录。

## 解决什么

- 当前代码、配置和日志仍是事实源，KB 只提供历史线索。
- 只在用户明确依赖历史记录、上次决定、以前项目经验时检索。
- 普通 worker 不直接运行 KB 脚本；检索、筛选、采用、写入和 closeout 由父会话统一管理。
- closeout 会记录命中、使用、确认采用和遥测缺失，避免把 AI 自报当成独立效果。
- `normal` 是默认模式；`challenge` 以风险触发和稳定 10% 抽样启动有限 critic，只生成 proposal，由主会话决定是否更新。
- `repo`、`branch`、`kind` 是一个物理根里的逻辑分类，不是多个数据源。

## 快速开始

1. 将这个仓库根目录作为 Skill 目录使用；根目录中的 `SKILL.md`、`agents/`、`scripts/`、`references/` 和 `backend/` 必须一起保留。发布包不含真实 KB 或本机配置。
2. 创建本机配置并指定唯一数据根。`config.json` 只留在本机，不要提交：

```bash
cp config.example.json config.json
export PERSONAL_KB_ROOT="$PWD/personal-kb-data"
```

也可以不创建 `config.json`，改用 `PERSONAL_KB_CONFIG=/absolute/path/to/config.json` 选择外部配置。配置中的 `records`、`retained_files`、`manifests`、`runtime` 和 `cache` 都必须是数据根下的相对路径。

3. 运行发布包内实际存在的检查；smoke 的参数是 Skill 根，不是数据根：

```bash
python3 scripts/kb_storage_test.py
python3 scripts/kb_challenge_test.py
python3 scripts/kb_retain_file_test.py
python3 scripts/kb_eval_preflight.py --routing-only --strict
python3 scripts/kb_smoke_test.py . --root-layout
```

不要提交 `kb.jsonl`、会话日志、简历或任何含本地绝对路径的文件。

## 设计原则

- RAG-first：只有跨会话依赖才触发检索。
- 证据优先：KB 命中必须对照当前权威文件验证。
- 低副作用：检索只读，成功 closeout 默认静默。
- 可审计：gold case、历史回放、closeout 和确认采用四层验证。

## 怎么证明有效

不要只给一个“采用率”。建议公开以下聚合口径：

| 指标 | 含义 |
|---|---|
| `preflight_gold_accuracy` | 冻结案例上的路由准确性 |
| `historical_replay_candidate_additions` | 真实会话回放中规则多识别出的检索点 |
| `self_reported_use_rate` | AI closeout 自报“命中且使用” |
| `confirmed_use_rate` | 有 adopted id 的确认采用率 |
| `session_brief_help_rate` | 短期 brief 实际帮助率，缺遥测时为 `null` |

当前本地脱敏示例见 `metrics.example.json`。它只是审计窗口的聚合示例，不是通用性能承诺。

## 隐私边界

默认不收集对话，不自动公开知识库，不把本地绝对路径写入发布包。项目证据可完整留在本地 retained-files，`kb.jsonl` 只放摘要和指针；公开导出由 allowlist 和敏感扫描共同把关，不包含真实 KB、retained-files、资产 manifest 或发布 ownership 状态。

数据库 schema/DDL/样本可用 `kb.py retain --category database` 留存，SSH/MCP 资源材料可用 `--category resources` 留存。完整归档也允许 `.env`、密码、Token、私钥和连接密钥，但它们是本地明文副本，仅在 POSIX 上尽力设置 owner-only 权限；这不是加密保险库。已经由 vault 或 secret manager 管理的材料可改用 `kb.py reference --reference-kind credential --locator <vault-or-secret-manager-item>` 只保存定位符。

## 当前限制

- `self_reported_use_rate` 是 AI 自报，不是独立人工评测。
- 历史回放是策略影子，不能证明检索改变了最终答案。
- 短期 brief 帮助率只有在 closeout 显式写入 `session_brief_hit` / `session_brief_help` 后才有意义；缺字段时只能报告遥测缺失。

## 许可证

本项目采用 [Apache License 2.0](LICENSE)，允许使用、修改、再发布、商用和闭源集成，并包含明确的专利授权条款。使用和分发时请遵守仓库根目录的完整许可证文本。
