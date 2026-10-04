# Personal KB 会话复盘与优化（2026-09-20—2026-10-04）

本轮延续 9 月 24 日的 Cursor 审计与 v2 设计，先修复实际使用链路，再考虑向量、图谱等扩展。审阅覆盖 82 个有内容的主会话（62 Codex、20 Cursor），另列 6 份自身 metadata 缺失的续接记录。每个会话检查用户请求、有界原文、助手结论、KB 调用及可用结果；没有逐字通读所有业务工具日志。原始材料和逐会话账本仅保存在本地审计目录。

## 发现、改动和验收

| 已确认问题 | 本轮实施 | 验收与边界 |
|---|---|---|
| 开发分支已有 session/topic 参数，实际入口却缺少；实际入口的 receipt/reliability 修复又未进入分支 | 先整合已安装实现，再恢复 session/topic 透传；新增 `kb.py version` | 对比实际入口与源码的 portable code fingerprint；保留本机配置、数据和映射 |
| 子 Agent 继承的父 metadata 覆盖自身身份，Cursor 子会话有根目录副本，旧会话续聊被时间过滤漏掉 | 统一自身 UUID 选择；缺匹配时标 unresolved；排除 Cursor 子副本；增加 `--include-continuations`，兼容 Cursor 英文显示时间戳 | 合成回归覆盖自身身份、未知身份、镜像、带日期续接和未知时间；当前历史重放与人工账本分母一致 |
| RAG、向量、提示词等业务词触发 Personal KB 治理查询扩展 | 保持偏召回的 Skill 触发，仅在明确 KB 维护意图下增加治理扩展 | 业务查询负例和 KB 优化正例同时通过；没有声称全库治理噪声已经消除 |
| 两个真实案例中，检索进程成功但工具包装仅输出 `.output`，丢失异步进程句柄，后续虚报零调用 | 运行指引要求保留完整返回并等待；审计拆分请求、进程结果、模型可见结果 | 裸 `Script completed` 不再代表 shell 成功；显式命令完成记录可显示结果未交付；不由此推断采用 |
| 批量 JS 包装、转义文本和嵌套结果漏解析 | 增加有界静态解析和按查询/等长有序结果匹配 | 不执行 JS；不确定表达式保持未知；防止 note 中的伪造 receipt 覆盖真实头部 |
| 拿不到 retrieval ID 时 closeout 诱导改成零调用 | 增加 `--unresolved-attempt ID=STATE`，真实调用数等于完成 ID 加未完成尝试 | pending/failed/unavailable/aborted/unknown 不冒充成功或命中；旧 linked-missing 指标保持既有口径 |
| 无写入无法区分没评估、证据不足、已有记录 | 可选 `--memory-review` 与原因 | written/updated 必须有相应条目 ID；评估字段本身不产生长期记忆 |

上述改动没有增加普通检索的网络调用或新数据库。新增成本主要是审计时解析更多事件，续接扫描可能读取较早文件，以及按需输出少量身份/评估字段。

## 修正原先的判断

- 本窗口 runtime 快照有 153 行事件，不能当作 153 个用户任务。早期有 4 个新增条目 ID、2 次更新；后期仍有 1 个新增。不能说近期零写入。
- 扩展结构扫描发现 28 个检索 ID 出现在执行完成事件，却未出现在模型可见工具输出。只有其中 2 个案例逐行确认了丢失异步句柄的因果链，不能把相同原因推广到全部 28 个。
- 一些闭环使用自动 closeout ID，仍能通过 linked retrieval ID 对应；帮助调用、中断、普通子 Agent、未知身份记录均不能自动判成漏闭环。
- `used-locate` 能体现找到当前权威材料的价值；命中、打开、采用、加热各有独立含义。

## 验证

- 新增 25 个针对性回归：身份、续接、业务/治理路由、异步完成、嵌套和转义回执、伪造 note、失败尝试、记忆评估及版本。
- Codex/Cursor 审计、runtime、effectiveness、P0 closeout、RAG/reliability、curation、stats、brief、敏感扫描、storage/challenge/retain、release 回归通过。
- routing-only strict、隔离 smoke、公开包 build/check 与 Skill 结构校验通过。测试保护器核对生产 records/runtime/manifests/retained-files 未变化。
- 初次失败记录保留在本地：既发现了真实的多结果/伪造回执兼容问题，也暴露旧测试对源码目录和外层 `Script completed` 的假设。修复后重跑受影响检查。
- 代码和指引已同步实际 WSL 入口；Windows Skill 的运行指引同步，默认执行仍指向 WSL。未覆盖本机配置和私有映射，安装前留有可回滚副本。

## 下一轮需要真实任务验证

本轮证明故障路径和接口回归通过，没有证明 Agent 今后一定遵循等待指引，也没有证明平均 Token、时延、任务成功率或长期采用率改善。后续从新真实任务按 session/topic 收集链路，重点检查结果交付、治理噪声和候选评估。业务修复候选需回查当前源码/验收证据后再决定沉淀，不批量导入旧对话，不回填猜测的历史成功事件。

使用说明见 [运行调用与闭环](../references/runtime-calls.md)，总体设计继续沿用 [memory system v2](memory-system-v2-design.md)。
