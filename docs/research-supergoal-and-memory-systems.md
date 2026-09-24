# Personal KB v2 外部方案学习记录

日期：2026-09-24
范围：`uk0/supergoal`，以及 Anda Brain、OpenMemory、Papr Memory、KGraphMemory、vektori 等公开项目的 README/架构说明。

## 先说判断

我们现在的 Personal KB 已经有单根数据、JSONL 事实记录、证据指针、倒排/时间索引、检索和 closeout，但它仍然是“高质量记录 + 关键词 RAG”。它还不是完整的可复用记忆系统，原因不是缺一个向量库，而是缺一条从**发生 → 记录 → 验证 → 关联 → 检索 → 反馈 → 淘汰/更新**的统一记忆生命周期。

本次不照搬任何项目，也不把外部项目的 benchmark 当成本地结论。保留的借鉴点如下：

| 来源 | 可借鉴 | 不直接照搬 |
|---|---|---|
| [supergoal](https://github.com/uk0/supergoal) | `SessionStart` 自动恢复；状态机；verify 命令退出码决定通过；append-only verified lessons；状态与运行证据冲突时以运行证据为准 | 它解决项目进度恢复，不是知识检索；不把 `.claude/state` 当长期 KB |
| [Anda Brain](https://github.com/ldclabs/anda-brain) | Formation / Recall / Maintenance 三段式；图关系和向量相似度并存；维护阶段处理压缩、冲突、过期 | 不引入 Rust/专用数据库作为第一步；其性能和功能声明不等于本机验收 |
| [OpenMemory](https://github.com/Tancy/openmemory) | 一个 canonical memory 对应多个记忆视角；分层记忆；稀疏连接和激活传播 | 不直接采用其 HMD 术语和服务拓扑 |
| [Papr Memory](https://github.com/Papr-ai/memory-opensource) | 文档、结构化数据、图、向量和权限的统一入口；实体关系自动发现 | MongoDB + Qdrant + Neo4j + Redis 对当前单机 JSONL 过重 |
| [KGraphMemory](https://github.com/vital-ai/kgraphmemory) | RDF/三元组 + 向量同步；显式 ontology；SPARQL 适合关系约束 | 不先引入 RDF/OWL 全套运行时；先用受限 JSON Schema 验证关系 |
| [vektori](https://github.com/vektori-ai/vektori) | 文件系统优先、事实/事件/原句分层、单库可解释 | 不把三层直接当成三个物理库；仍保持单一 canonical root |

## 对我们的直接启发

1. **单入口必须是生命周期入口，不只是 `retrieve` 的别名。** 所有需求、bug、运行时命中和验证结果先进入 `memory event`，再按证据状态进入长期记忆。
2. **自动保存与自动可信不能混为一谈。** “保存下来”可以是候选/事件；“以后可作为事实使用”必须有证据、状态和有效期。
3. **恢复必须由宿主触发。** 每次 Cursor/Codex 启动、恢复、压缩后，应由 hook/adapter 自动生成当前项目的 resume block，不能等模型想起读某个文件。
4. **验证是记忆升级门禁。** 需求由用户确认，bug 由当前代码/日志/测试复现，运行时结论由真实命令或接口证据确认；模型自述不升级状态。
5. **图谱先做关系，不先做数据库。** 先用 `supports / contradicts / supersedes / derived_from / applies_to / caused_by` 六类关系，基于 JSONL 重建邻接索引；关系稳定后再评估 RDF/图数据库。
6. **向量是候选召回层。** 不能让 embedding 结果直接覆盖当前证据、时间有效性、项目范围或冲突状态；最终排序必须是 lexical + vector + graph + freshness + evidence 的混合分数。

## 不采纳的误区

- 自动把全部聊天直接写成长期事实：会把临时猜测、错误命令和旧结论一起放大。
- 先上三套数据库再补生命周期：会把当前的归因、验证和冲突问题藏到基础设施后面。
- 只看 Recall@K：命中旧事实仍可能是错误答案；要测“是否改变决策、是否避免错误、是否减少重复验证”。
- 把文档状态当事实：`STATUS.md`、memory brief 和 runtime closeout 都是证据来源或过程状态，不等于当前事实本身。
