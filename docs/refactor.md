# 记忆感知与多模态引用

## 生产、保存与消费

每个外循环结束仅保存原始事件，包含真实用户输入、实际结果、工具轨迹及不可变引用 ID。不会先生成占位情景，也不会把原始对话直接切片入库。

自动感知默认每累计 20 个新增 user-assistant 外循环回合触发一次；附带前 3 回合作为衔接，不占新增额度。例如首次消费 1–20，随后消费 21–40 并附带 18–20。工具调用、重试和同一回合中的加急补充不增加回合计数。清空会话、切换项目时可以处理新增短窗口；退出只封存未处理窗口、取消正在进行的任务，重启后恢复，没有新事件时不调用 LLM。

`MemoryPerception` 是独立的子 Agent 任务，通过 `memory_perception.model_role` 选择配置，默认复用 Worker。上下文压缩只使用 Compressor。感知共用一个由配置限制并发的工厂；旧项目感知保持原上下文在后台完成，目录切换不等待它，结果不进入新项目会话。任务子 Agent 使用自己的工厂，避免全部任务在等待主动记忆时占满感知执行槽。每窗执行一个完整感知 Agent，通过检索、读取工具补充原始证据，最终提交一次结构化结果；不另启分片摘要模型。每窗重新提供关联原始图片，视频本轮排除。

| 数据 | 位置与作用 |
|---|---|
| 原始观察 | 用户数据目录 `projects/<project_id>/memory/turns/*.json`；`event_order.json` 保存完成顺序，`perception_state.json` 保存消费游标 |
| 原始轨迹 | 用户数据目录 `projects/<project_id>/*.jsonl`；`*_ModelMessages.json` 是可加载的上下文视图 |
| 引用清单与快照 | 用户数据目录 `references/manifests/*.json` 和 `references/blobs/`，保留来源、校验和、原件快照及格式解析结果 |
| jobs 与处理状态 | `projects/<project_id>/memory/jobs/*.json`、`processor.json`；迁移任务备份在 `LongTermMemory/migration_backup/jobs/` |
| 正式记忆 | LanceDB 表 `memory_records_v3`，同一张表按 `project` / `global` scope 隔离；完整记录由此表拥有 |
| 核心文档 | `LongTermMemory/MEMORY.md`，会话开始时完整注入并固定快照，不设字符硬限制；后续更新通过结果与检索消费 |

`remember` 使用 LLM 处理真实用户主动要求，不等自动窗口。返回记录 ID、scope 和实际保存状态；失败请求保留并明确报错。`search_memory` / `read_memory` 消费当前项目及本人全局资料；`search_episodes` / `read_episode` 保留为项目情景专用入口。

自动感知可以输出零条、一条或多条记忆；闲聊、能力介绍不必形成情景。生产变更必须关联新增事件，不能仅凭 overlap 重放；用户主动记录和删除状态防止旧事实复活。失败、取消或无证据的操作不能作为成功经验。

正式记忆只写入 LanceDB `memory_records_v3`；`.json` 文件只承载原始观察、引用清单和 jobs/处理进度，模型上下文快照仍属于可加载的原始轨迹。`MEMORY.md` 只保留画像与通用经验的核心投影，详细项目资料和长期知识通过全局 RAG 记录召回。

## 运行时边界

终端和机器人会话共用 `runtime.session.TurnQueue` 的 FIFO 接纳与取消语义；每个会话有自己的队列，取消一个回合不会杀掉队列消费者。`SessionController` 的 urgent inbox 只合并当前回合后续输入，不改变回合边界。

浏览器工具由异步、懒启动的 `PlaywrightBrowserSession` 持有。页面动作在会话锁内串行执行，资源归属 Agent 的事件循环，并在 Agent 关闭时释放；子 Agent 使用自己的事件循环资源。

子 Agent 的执行角色由工厂设置，命令与 Skill 脚本共用启动前的权限检查。`execution.permissions` 配置受限角色、进程控制命令和脚本检查规则；常见 kill/taskkill/Stop-Process/Python 进程终止操作会被拒绝。端口占用不代表进程归自己所有，任务取消和自有进程树回收由运行时负责。这是工具层的误操作防护，不提供操作系统沙箱，不使用 Windows 权限库或系统 Hook。

CLI 退出首先关闭任务接纳、取消执行和感知请求，保存未完成窗口后释放资源。正常退出不等待模型产出；如果线程中的阻塞调用不响应取消，`lifecycle.shutdown_grace_seconds` 限定退出清理时间，原生 Python 退出入口结束当前应用实例。该限时只在退出阶段启动，不是任务运行时限。

`SkillsManager` 负责合并随包 Skills 与用户 overlay，`BasicToolkit` 直接把 `SkillsManager.tools` 暴露给工具组，指令、资源和脚本按需读取或执行。CLI 与 TUI 共用 `AgentSystem.process_cli_line` 的命令处理，并共用 `cli.completion` 的命令帮助、命令集合和 `completion_for_input` 补全逻辑。

## 配置与资源

开发入口 `main.py` 显式读取项目配置，pip 与冻结程序使用本机用户全局配置。可用 `REDLOTUS_CONFIG_FILE` 或 `REDLOTUS_CONFIG_DIR` 覆盖来源。`REDLOTUS_DATA_DIR` 改变包括项目观察在内的全局数据根，项目产物仍属于当前项目的 `WorkDatabase`。

模型、服务、采样/推理、输入预算与 RAG 参数由 JSON 配置提供，运行时使用独立深拷贝。感知的模型、输出上限、上下文和引用预算均来自 `memory_perception.model_role` 所选角色；压缩配置不影响感知。完整参数结构与预设示例见 [网关与安装说明](gateway-installation.md)。

```json
{
  "memory_perception": {"window_turns": 20, "overlap_turns": 3, "model_role": "worker", "max_concurrent": 1},
  "input_limits": {"defaults": {"max_file_bytes": 20000000}},
  "long_term_memory": {"table_name": "semantic_memories"}
}
```

以上为非模型策略。旧 `char_limit` 及逐轮 consolidation 参数退出使用。原有 RAG 参数继续生效：`short_term_memory.db_path`、`table_name`、`vector_search_limit`、`final_top_k`、`use_rerank`、`min_similarity`、`turn_token_limit`、`turn_chunk_overlap_tokens`，以及 `index.min_rows`、`index.metric`、`index.rebuild_every_n_adds`；顶层 `RAG_models.embedding` 和 `RAG_models.reranker` 也继续生效。

RAG 向量索引表使用 `<table_name>_records_v2_<embedding模型哈希>` 命名；它是 `memory_records_v3` 正式记录的可重建索引，不是另一套记忆库。完整记忆优先作为索引单元；超预算才分块，避免重复尾块。同批正文变更批量 embedding，元数据变化不重新向量化。仅在确有旧分块时清理，索引成功后推进索引状态。

生产与索引重试分离，job 保存封窗范围、模型配置快照、生产结果、检索记录、用量和分阶段耗时。模型失败或被取消时不推进完成位置；成功生产的结果先持久化，索引失败只重试索引。新增回合或重启补处理未完成任务；HTTP 400/401/402/403 在当前运行实例暂停自动请求，重启或手动 `/STM retry`、`/LTM retry` 后再尝试。同一 job 的前台保存和后台恢复通过执行锁复用一次生产结果。并发更新使用文件锁，线程、事件循环及客户端由工厂关闭。退出实例固定自己的处理终点，不接管之后新增的事件。

用户纠正按原始事件时间及请求登记时间排序；提交较晚的旧请求不能覆盖新纠正。清空记忆保存对应范围的时间边界；清空前的请求和缓存生产结果不再写入该范围。Windows 下 LanceDB 的过深路径会改用用户本地目录中的稳定短索引路径，原始记忆保持原位，已有索引保留。

LanceDB 目录来自配置；隔离测试可通过显式进程环境变量 `RAG_DB_PATH` 覆盖。运行时不再通过 Win32 API 猜测卷类型并更换数据库目录。Windows 的数据库应配置在支持 Lance 事务的 NTFS 目录；评测中的 exFAT E 盘只保存资料、代码和证据，数据库保留在配置指定的 C 盘目录。

## 引用文件

支持 `@路径`、`@"含空格路径"`、`@{路径}` 和明确的图片/视频路径。只解析用户原始输入，不扫描引用文件正文中的其他路径。每次最多 20 个规范路径去重后的文件；网关未声明大小时单文件上限为 20,000,000 字节。

文档读取保留页码、表格、工作表、行列关系、公式、幻灯片、图片和备注。旧 DOC/PPT 使用 LibreOffice 无界面转换，XLS 使用文档读取器。图片按 SDK 原生多模态内容传递；视频尚未完成真实协议验收。引用内容带“引用文件”和来源标识，不冒充用户指令。

超数量、大小、缺失或格式错误明确返回失败，不能偷偷丢文件。视频识别本轮按用户要求排除，不能把视频协议编码、本地容器检查或文本描述当作视频识别通过。

LibreOffice 可使用系统安装，或设置 `LIBREOFFICE_PATH` 指向 `soffice.com` / `soffice`；不依赖开发仓库中的工具路径。转换使用独立配置目录、禁用宏和可取消的进程树，不修改用户 Office 配置。

## 迁移

迁移先备份可识别的旧情景、处理状态、会话和旧 LanceDB 向量表。旧正式 JSON 记录只作为一次性迁移输入，备份并去重后导入 `memory_records_v3`；旧 episodes/pending 状态、历史快照和旧索引内容转为 `origin=legacy` 的待感知观察事件，必须经过 LLM 再生产到 `memory_records_v3`。无法确认项目的旧向量保留在隔离清单，不参与召回。

迁移任务失败或中断时保留原始输入、job 进度和备份，重试沿用同一任务结果，不直接把旧 JSON 内容当作正式记忆。旧 MEMORY.md、USER.md 或 SOUL.md 的内容由 LLM 重新归类：详细项目与知识进入全局 `memory_records_v3`，画像和通用经验保留为 `MEMORY.md` 核心投影；原件备份可追溯。

旧核心文档的迁移输入与 job 结果保存在全局迁移备份目录，部分提交失败后继续应用同一结果，避免依据已修改正文重复提炼。

## 验收入口

```powershell
uv sync --extra browser --extra dev
uv run playwright install chromium
uv run python scripts/real_acceptance.py --root <全新的NTFS测试目录>
```

运行前需准备 LibreOffice；可使用系统安装，或设置 `LIBREOFFICE_PATH` 指向 `soffice.com` / `soffice`。上述命令验收实际 CLI 控制器、Textual 界面、模型和工具；模型配置只读，测试存储隔离，请求审计记录真实用量与元数据，不伪造响应。视频用例默认关闭，本轮不使用 `--include-video`，不报告视频识别通过。

```powershell
uv run python scripts/check_references.py --root WorkDatabase/reference-validation
```

该命令验证真实本地文件、Office 转换、引用数量与快照，不代表模型识别通过；其中的媒体检查也不代表视频识别通过。pytest 和静态检查仅作为辅助回归。

本轮真实范围见 [2026-09-14 验收记录](acceptance-2026-09-14.md)，此前安装验收见 [历史记录](acceptance-report.md)。辅助测试数量不能替代真实验收。
