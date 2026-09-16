# 五板块重构功能对照与验收边界

基线为 `a30192a3` 加本轮开始前已有工作区修改。以下是本轮执行记录，不是全部通过声明。
所有模型验收使用实际配置和真实服务；辅助测试中的 FunctionModel、人工调度夹具不计为真实验收。

## 功能对照

| 功能及入口 | 必须保持的结果 | 本地行为证据 | 本轮真实覆盖及缺口 |
|---|---|---|---|
| 普通输入、连续输入 | FIFO，回复归属正确，不丢失或重复 | `test_system`、`test_entries`、`test_acceptance_driver` | `live-v3` 完成同一会话 60 个真实用户回合 |
| `/urgent` 与附件解析 | 原始边界和提交顺序保留；不进入已切换会话 | `test_runner`、`test_usage_input`、`test_reference_admission` | 第 10 回合分六次提交加急，登记及消费顺序一致，同属一个回合；慢附件组合仅辅助验证 |
| `/stop`、Ctrl+C、`/clear`、`/cd` | 取消实际所属任务，保留独立普通队列；迟到回调失效 | `test_entries`、`test_control_receipts`、`test_usage_input` | 真实退出取消未完成感知；其他组合未全部实测 |
| `/load`、项目进入选择 | 恢复原 session ID、上下文、提示词、任务、计数和引用 | `test_recovery`、`test_session_file`、`test_entries` | 第 19 回合退出并真实加载；加载没有模型请求；下一回合形成 1–20 窗口 |
| `@` 引用、补全及去重 | 中文/空格/相邻引用；20 个可接纳、21 个拒绝；错误可修改 | `test_file_refs`、`test_reference_spaces`、`test_reference_admission` | 真实 CSV、Markdown、含空格路径和随机图片；20/21 边界仅辅助验证 |
| PDF、Word、Excel、PPT、HTML 解析 | 保留页/表/图片/公式/备注和出处；不静默截断 | `test_document_structure`、`test_components`、`test_missing_reference_boundary` | 本轮未对所有格式发真实模型请求；旧 Office 转换依赖仍须另验 |
| 原件与图片恢复 | 不可变引用快照；加载后传递原件 | `test_session_file`、`test_storage_routing`、`test_compression_reference_sources` | 随机图片识别成功，原件快照保留；视频按之前确认暂缓 |
| diff 展示、保留与撤销、审查 | 保留实际文件修改块，处理外部修改冲突 | `test_components`、`test_presentation`、`test_entries` | 文件级及 Textual 组件级辅助验证；本轮未完成全部真实终端审查操作 |
| goal 模式 | 多次内部迭代仍是一个真实用户回合；约束和状态继续传递 | `test_entries`、`test_system`、`test_context` | 本轮 60 回合场景未启用 goal；不视为真实通过 |
| 工具、Skills、子 Agent | 工具能力、渐进加载、项目范围、指定运行环境不变 | `test_components`、`test_execution_environment`、`test_usage_execution`、`test_command_permissions` | 子 Agent 实际编写并运行 `summary.py`，独立读取 `totals.json` 核对总额 1000 |
| 浏览器、图像生成及其他媒体工具 | 注册、参数和原有能力保留 | 工具注册/协议及文件辅助检查 | 本轮未逐项连接外部服务，不标记真实通过 |
| 网关、`/agent`、并行工具 | 原协议配置保留；请求边界切换不重做已完成工具 | `test_gateway`、`test_switching`、`test_model_selection`、`test_reasoning` | 现有服务真实请求；其他协议与跨服务切换未真实覆盖 |
| 提示词和上下文压缩 | Skills、系统信息、固定会话记忆快照不缺失；工具关系合法 | `test_prompt_cache`、`test_context`、`test_long_turn_compression` | 主 Agent + 普通子 Agent 缓存 99.19%；本轮未触发三次真实自动压缩 |
| 自动感知 | 当前 session，每新增 20 回合封窗；overlap 3，不额外收尾 | `test_session_windows`、`test_memory_sealing`、`test_perception_ownership` | 60 回合恰好三个窗口；首次固定验收截止时仅首窗完成，未通过全部生产要求 |
| 主动记忆、纠正、遗忘、RAG | 显式请求即时生产；项目隔离；索引失败不重做已成功生产 | `test_memory`、`test_memory_scope_update`、`test_rag`、`test_perception_runtime` | 项目别名真实 LLM 保存，情景真实 embedding/rerank 召回；其他质量场景未全覆盖 |
| 会话存储与统计 | 一个增量 JSON；清理原文后累计用量仍可查询 | `test_session_file`、`test_session_statistics`、`test_cache_accounting` | 60 回合约 0.62 MiB 单文件；恢复完成后 538,133 字节；控制回执误计响应已修复，原始失败保留 |
| session 线程额度 | 同一 session 上限 16；排队不先建线程，工具无额度死锁 | `test_session_thread_limit` 实际创建 32 个任务并核对线程 | 真实业务最高一个工厂线程；16 线程压力验证是本地线程测试 |
| `/help`、`/status`、`/config`、`/context`、`/usage`、`/panel`、`/tasks`、`/trace`、`/skills` | 命令接纳、展示、统计来源与副作用保持 | 命令、状态及展示相关辅助检查 | 本轮未逐个进行真实终端操作 |
| `/api`、`/effort`、`/compress`、`/cancel`、`/LTM`、`/STM` | 编辑/取消、压缩、控制回执和记忆操作不变 | 配置、命令、压缩、回执及记忆辅助检查 | 模型/RAG 参数未改；`/STM retry` 的真实恢复单独记录 |
| QQ/微信入口及本人权限 | 使用同一会话接口；非本人不读写个人全局记忆 | `test_entries` 的 Bot 适配和权限测试 | 本轮未连接真实 QQ/微信账户 |
| 源码、pip、冻结入口 | 配置属于本机用户；资源只读；包内无个人配置/记忆 | `test_installation`，包括独立进程导入检查 | 已做 wheel 实际安装及仓库外导入；复用既有依赖，未宣称干净环境或 PyInstaller 全功能通过 |

## 固定真实验收的实际结果

`live-v3` 在 570.5 秒结束，完成 60 回合、83 次模型 HTTP 请求，没有 WARNING/ERROR。
第 20、40、60 回合分别封存 1–20、21–40、41–60 窗口；后两窗分别附带 18–20、38–40。
首次运行只有第一个自动窗口生产和索引完成，因此整体失败，原始失败记录保留。

首窗端到端 164.72 秒，其中模型 API 162.67 秒，其他处理 2.05 秒。
第二窗等待首窗后开始，在退出时被取消；第三窗尚未开始请求。
这些数据证明本次主要耗时来自 API 和顺序等待，不能推断所有长期运行场景都没有调度问题。

首次运行主进程峰值 RSS 为 286.55 MiB，总 CPU 时间 58.67 秒；最高 84 个操作系统线程，
其中工厂 Agent 线程最高一个。两者口径不同，不能用 OS 线程数代替 session Agent 线程数。
主进程退出后已确认不再存在。本轮不足十分钟的资源观测不能代替数小时泄漏测试。

恢复运行 `live-v3-recovery-v1` 在 434.72 秒内通过：实际调用 `/load`、`/STM retry`，
使用 6 次真实模型请求完成其余两个窗口。没有创建额外窗口、没有增加用户回合、没有重做
已完成生产，三个窗口的情景 ID 一致，更新及 RAG 召回均有真实记录。原来的失败保持不变。

本轮最终辅助回归 `core-full-v3.txt`：306 通过、1 失败，237.75 秒。
结构失败为 core 10 个文件超出五文件上限；所有单文件符合 500 有效行要求。

## 仍未满足的交付门禁

1. core 文件数量尚未达到每板块五文件，不能以其他板块剩余容量抵消。
2. 首次真实固定验收未在期限内完成所有感知生产；后续恢复不覆盖首次失败。
3. 全功能真实交互、各协议/渠道以及长期压缩验收未全部完成。
4. 函数职责清单已有对应功能，但仍有缺失单一职责说明的条目，不标记完成。

用户随后明确要求“先合并进入 develop”，本次按该指示集成当前结果，
不表示以上剩余门禁已经通过。正式旧会话存档尚未删除。
原始测试、配置指纹、逐文件代码量和函数清单位于
`E:\代码\Agent\WorkDatabase\evaluation\session-refactor-20260916`。
