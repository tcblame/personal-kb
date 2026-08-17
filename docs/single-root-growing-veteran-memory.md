# 单根 KB 与可成长老员工记忆层

## 决策

`personal-kb` 只有一个物理数据根。`repo`、`branch`、`kind` 只是检索、排序和治理用的逻辑分类，不是多个数据源。默认主会话负责检索、核验、采用、写入和 closeout；普通子 Agent 只消费父会话筛选后的线索。

系统定位为“可成长的老员工”：

- 用户明确说要记住的规则、决定、映射和经验必须进入长期 KB。
- AI 在任务中发现经过当前证据验证、稳定且可复用的 bug 根因、设计取舍、资源映射或工具经验，可以主动写入或更新。
- 临时观察、猜测、未验证结果和整段聊天不进入长期 KB。
- 当前代码、配置、日志、数据库现场和用户当前指令永远高于历史命中。

## 路径合同

配置中的 `storage.root` 是唯一根；其下相对路径分别定义：

- `records`：唯一长期检索源，保存精简 JSONL 结论。
- `retained_files`：完整本地项目证据和大文件。
- `manifests`：资产 ID、大小、hash 和本地 origin/stored 路径。
- `runtime`：closeout、采用事件、session brief、challenge 和效果审计。
- `cache`：倒排索引、时间索引和聚合视图，可随时重建。

历史安装可以让 runtime/cache 指向旧目录以保持连续性；portable 发布配置使用互不重叠的 `runtime` 与 `cache` 目录。任何子路径都不能逃出根，也不能回退到当前工作目录或历史 Windows 路径。

## 证据与敏感信息

用户选择全量项目证据归档时，数据库 schema、关系、字段含义、必要 DDL、样本、内部地址和 SSH/MCP 资源文件可以原样保存在本地 `retained-files`。`kb.jsonl` 只记录结论、opaque `asset_id` 和相对证据指针，避免把大文件正文塞进主库。retain 默认 copy，原始位置可能继续存在，不能称为安全删除。

用户后续明确决定“所有信息全部保存”。因此本地 `retained-files` 允许原样保存仍有效的密码、Token、私钥、连接密钥、`.env`、数据库连接文件和其他项目资料，不做内容过滤或自动脱敏。`kb.jsonl` 仍只保存精简结论、opaque `asset_id` 和相对证据指针，避免把大文件及秘密复制进默认检索输出；需要精确内容时再读取本地资产。`reference` 只用于本来就位于外部系统的资源定位符，不再是保存活凭据的强制替代方案。

第一版不引入空壳 vault 抽象，也没有“回答密码后解锁”的能力。本地归档是明文 copy，POSIX 下只做尽力而为的 owner-only 权限收紧，不能宣称加密、Agent 隔离或安全删除。公开导出不包含真实 KB、retained-files、manifest、runtime 或绝对路径，并继续用 allowlist 和敏感扫描阻止本地内容进入发布树。

## 两种运行模式

`normal` 为默认模式，追求低 token 和低副作用。`challenge` 在风险词命中时同步触发；普通成功任务按稳定 task ID 哈希默认抽样 10%。每轮最多质疑 3 条实际采用记录，critic 只产生 depth=1 proposal，不写库、不加热、不 closeout、不递归。主会话对 proposal 与当前证据核对后，才决定 accepted/rejected/deferred，并自行更新记录。

实现门禁要求非 `--force` 的 challenge 候选必须能匹配本轮 material adoption event；普通检索命中和 locate-only 事件不能冒充“已采用”。proposal 必须来自已 enqueue 的 brief，并提供当前证据、`keep/correct/supersede/defer` 动作和原结论失效原因。

错误分类固定为 `record_error`、`retrieval_error`、`scope_error`、`application_error`、`evidence_error`、`outcome_unknown`，并要求记录“为什么原记录导致错误”和验证依据。

## 结果

日常入口统一到 `kb.py`，维护和验证入口分别为 `kb_admin.py` 与 `kb_eval.py`。审计器同时识别旧 focused scripts 和 wrapper commands，迁移入口不会伪造检索率下降。公开发布由 allowlist exporter 生成独立工作树，不从真实数据根复制文件。

## 不在本版本做

- 多个物理 KB 或强制按项目拆库。
- 自动记住全部聊天。
- 让 critic 直接修改长期 KB。
- 内置密码保险库或假装目录权限等于 Agent 隔离；本地完整归档与密码保险库是两个独立能力。
- 让向量索引或聚合缓存成为事实源。
