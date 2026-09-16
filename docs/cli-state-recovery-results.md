# CLI 状态与事务恢复修复记录

基线为 `develop@fb1260ff`，实现位于 `codex/cli-state-recovery`。本记录区分真实模型验收、本地故障注入和结构限制；不把辅助测试当作真实服务通过依据。

## 修复与职责

| 用户功能 | 原因与修复入口 | 验证方式 |
|---|---|---|
| 切换期间输入归属 | 按用户后续确认改为切换期间锁输入框、保留草稿。`AgentSystem.switch_workspace` 先准备目标，再释放旧资源；初始化或释放失败时保留原 session。控制器的读取与绑定仍处于同一接纳边界。 | 慢附件、慢文件读取、慢绑定、失败切换、普通任务队列；TUI 锁定另列 |
| 提问时加急回复 | TUI 的普通提交和 Ctrl+Enter 共用回答入口，完整保留补充要求。 | Textual 输入测试，不冒充物理终端按键验收 |
| 手动压缩 | `prepare_compression` 返回独立候选；`AgentSystem.compress_context` 使用已有 `TurnQueue`；两个角色候选同批保存后才更新历史。 | 真实请求另列；本地测试覆盖重复命令、排队、停止、迟到候选和双角色保存失败 |
| 存储暂停 | `_durable_write` 只重复文件操作；`retry_saved_state` 由下一条普通输入触发。暂停不会重新请求模型或执行已经完成的工具。 | ENOSPC / 权限错误注入，包含实际 AgentRunner 控制链 |
| 单文件事务 | `SessionFile._append/_write_update` 为增量批次增加序号和 SHA-256；刷新成功才发布。`retry_pending` 先确认未决批次，禁止后续写入覆盖。 | 半个中文字符、末尾 JSON、校验失败、fsync 失败、重复重试 |
| 中断恢复 | `_read/_recover` 按 JSON 顶层对象与提交校验识别后续完整事务，不依赖换行；中间损坏保留原文件并报告。`repair_interrupted_tool_calls` 补充未知回执，`bind_loaded_snapshot` 用原 `turn_id` 保存关联，不重放工具。 | 中间连续损坏、尾部缺闭合、字符串与嵌套内容反误判、二次加载幂等、工具 ID 配对 |
| 加载与清空 | `SnapshotSelection` 区分新建、恢复和取消；`bind_loaded_snapshot` 保留 ID 与文件，清空通过 `MemoryService.unbind_session` 解除旧证据与感知绑定。 | 三态选择、恢复继续写、清空后当前会话计数 |
| 空间不足清理 | `retry_after_storage_cleanup` 先按时间清理七天以前的可删除会话，再清理明确归属且未使用的执行缓存；活动锁、待处理任务及无法确认的数据受保护。候选采用 `SessionFile.load(recover=False)` 只读验证，避免清理过程中修复文件并递归清理。 | 隔离目录、真实文件锁、删除后逐次重试、非空间错误不删除、半写候选原字节不变 |
| 记忆命令和工具错误 | `/LTM` 读取 `MemoryReader`；非法/缺失 ID 返回可恢复业务错误。`/STM` 分别显示项目数量与当前会话进度。 | 读取、错误范围、清空解绑、命令渲染 |
| 长工具结果与帮助 | 完整结果保存在所属项目 `WorkDatabase/tool_results`；帮助参数尖括号按 Markdown 正确转义。 | 实际产物路径及可读性、帮助文本 |

普通队列、AgentRunner 和工作区辅助代码按原职责重新归类；没有新增会话数据库或调度框架。文件清理不会删引用原件、项目产物、正式记忆、RAG、配置、源码或依赖环境。

## 配置

随包 `config.json` 仅新增：

```json
"storage": {
  "cleanup": {
    "enabled": true,
    "session_retention_days": 7,
    "execution_cache": true
  }
}
```

原有配置初始化入口在补全字段前会保存配置备份。正式用户配置未被本轮测试改写；真实验收使用独立副本，只覆盖存储目录。模型、RAG 服务参数、输出长度、压缩阈值和固定提示词保持基线。

正式配置测试前 SHA-256：`5A1D90D671C1F273428254BAD124D972ABBFCAC58F866DDF92E3F30C23D5CA02`。

## 验证记录

证据根目录：`E:/代码/Agent/WorkDatabase/evaluation/cli-state-recovery`。

- `transactions-red.txt` 与 `transactions-green.txt`：首组六项缺陷的失败证据，以及修复后的 20 项会话测试。
- `leases-red.txt`：未决批次被第二次写入替换、缺少活动会话保护的失败证据。
- `middle-red.txt`：中间 JSON 损坏被误当作尾部中断的失败证据。
- `creation-red.txt`：首次创建没有 durable flush 的失败证据。
- `commit-view-red.txt`：写入确认失败后读接口提前发布新状态的失败证据。
- `full-local.txt`：完整本地回归第一次运行，**355 通过、4 失败、512.75 秒**。两项为重构后的测试替身/路径适配，两项为结构限制；后续修复及最终结果另列，原始结果保留。
- `final-storage.txt`：最终事务、压缩、内存可见性、清理和记忆边界定向组，**58 通过、39.47 秒**。
- `switch-red.txt` / `switch-green.txt`：目标组件初始化或旧工具释放失败丢失原 session 的两个复现；修复后连同正常切换回归，**5 通过、12.90 秒**。
- TUI/输入组：五项新口径失败先复现，修复后 **14 项功能测试通过**。锁定期间触发迟到的 Ctrl+Enter 不清掉草稿；解锁后仍为原 session。
- `live-01/`：39.95 秒停止，未发出模型请求。隔离配置没有显式关联本机凭据文件，属于脚本准备失败；后续批次显式传入原全局 `.env`，没有修改凭据或模型配置。
- 最后补充事务组：**23 通过、15.82 秒**，涵盖无换行及连续损坏、末尾缺根括号、正文内伪事务反误判、真实观察事件 ID 与用户回合 ID 的恢复关联。
- 最后清理组：**6 通过**，包括半写旧会话不重写、不删除、不递归进入清理。
- `live-02/`：**222.30 秒、21 次真实模型请求、0 个非预期 WARNING/ERROR**。两项真实工具产物、六条独立加急、手动压缩期间排队、同文件同 ID 加载和早期资料追问均已执行。原脚本将 `ObservedTurn.id` 错当 `turn_id`，保留原始失败报告，正确核对另存补充证据。
- 主 Agent 实际缓存命中为 **215,296 / 336,812 = 63.92%**，**未达到 90%**，不通过增加回合或修改配置凑指标。辅助模型用量另列。
- `live-02/reports/supplemental-validation.json` 保留原报告和会话文件哈希，并重放第 22 个提交：首轮完成时计数恰为 1，原输入及六条加急顺序一致，11 个工具调用/返回 ID 完整配对。原误报未被覆盖。
- 实际压缩模型请求耗时 **17.687 秒**；压缩加后续排队任务合计 **28.281 秒**，生成 1 条有效摘要。加载用时约 **0.219 秒**，会话 ID 和恢复文件路径均不变；清空后没有新增模型请求。
- 主 Agent **19 次请求、336,812 输入 token、29,144 输出 token**；压缩 **1 次、5,933 输入、3,999 输出**；标题 **1 次、374 输入、15 输出**。这些用量来自真实响应，未用估算替代。
- 206 个资源采样点：进程树峰值 RSS 约 **276 MiB**、累计 CPU 时间峰值 **42.89 秒**、系统线程峰值 **78**；本批没有启动普通子 Agent，Agent 工厂线程峰值为 **0**。观测到的相关 PID 在结束后均不存在。数字包含验收采集开销，不据此宣称长时间无泄漏。
- 所有主 Agent 请求的 system/工具定义前缀哈希一致；同回合 15 次请求的缓存命中仍为 **63.96%**。不能把未达标全部归于冷启动，也没有证据认定 system 提示词发生改写。
- 进一步比较 15 组同回合连续请求：前一请求的完整 `messages` 前缀均保持相同，只在尾部追加。未观察到旧 assistant 的 `content` 或 `reasoning_content` 重写。低命中率原因尚未确认，不据此修改提示词或服务参数。
- `final-all.txt` / `final-all-duration.json`：最终全量本地回归 **374 通过、2 失败、416.41 秒**；唯一失败类别为文件数量和单文件有效代码行数。本批无未完成测试，低于 600 秒上限。

本地 `FunctionModel` 只用于故障控制，不计为真实服务调用。真实批次之后补的损坏文件、只读清理和中断回执关联修复采用本地故障注入及最终全量回归验证，未另行重跑真实 API；源码指纹可区分两个版本。

## 尚未通过与边界

- `core` 仍有 10 个 Python 文件，超过旧有五文件上限。
- 本轮新增超限为：`config.py` **683**、`session.py` **533**、`system.py` **522** 有效行（基线分别 449、492、487）。不能归为既有问题；未放宽结构断言。`console.py` 496、`tui.py` 497。
- 主 Agent 小批真实缓存命中率 **63.92%**，未达到 90%。
- 用户已确认切换期间锁输入框、失败保留原 session；无需增加多条失败输入编辑列表。
- 本轮没有实际操控 PyCharm / Windows Terminal 的物理按键，也没有执行 pip / PyInstaller 打包验收。
- 真实批次为本轮 CLI 修复校准，不是六十轮感知或长时间性能认证。未重测的历史功能不标为本轮真实通过。

本轮 CLI 功能修复和故障回归已交付；结构与缓存验收未满足，因此不合并、不推送 `develop`，不删除工作分支。主工作区仍可继续使用原版本，修复在 `E:/代码/Agent/WorkDatabase/session-refactor-worktree` 的 `codex/cli-state-recovery` 分支。

## 复现入口

从本工作分支的根目录运行。真实验收使用原全局配置及凭据文件，只在独立测试区域保存记忆；`--root` 每次选择新的 E 盘目录，避免覆盖失败证据。

```powershell
& 'E:\代码\Agent\.venv\Scripts\python.exe' scripts/cli_state_acceptance.py `
  --config 'C:\Users\Administrator\.redlotus\config.json' `
  --dotenv 'C:\Users\Administrator\.redlotus\.env' `
  --root 'E:\代码\Agent\WorkDatabase\evaluation\cli-state-recovery\live-next' `
  --dependencies 'E:\代码\Agent\WorkDatabase\evaluation\session-refactor-20260916\runtime\dependencies' `
  --deadline 600
```

本地故障注入入口为 `tests/test_session_transactions.py`、`tests/test_cli_storage_transactions.py`、`tests/test_storage_cleanup.py`、`tests/test_cli_transition_recovery.py`、`tests/test_manual_compression.py` 和 `tests/test_memory_cli_boundaries.py`。测试配置隔离由 `tests/conftest.py` 执行；这些测试禁止真实模型请求。

## 清理辅助函数与需求映射

| 函数组 | 对应用户功能及单一职责 |
|---|---|
| `_storage_full` | 仅空间不足触发清理，区分 Windows 错误号与 POSIX errno |
| `_ordinary_storage_path`、`_storage_children`、`_safe_storage_child` | 只枚举可确认归属的普通目录和文件，不跟随链接删除外部目录 |
| `_same_storage_volume` | 只释放失败文件所在卷的空间 |
| `_locked_storage_file`、`_release_storage_lock`、`_locked_session` | 持有检查和删除窗口所需的事务锁、会话使用锁 |
| `_session_candidates` | 按七天截止时间筛选并排序历史会话 |
| `_session_protected` | 判断身份、活动回合和待处理感知状态，无法确认时保留 |
| `_delete_old_session` | 再确认时间和锁后，仅删除会话恢复文件并返回释放量 |
| `_active_cache_projects` | 防止清理仍被当前实例使用的项目缓存 |
| `_execution_storage_root`、`_delete_owned_cache` | 限定配置缓存范围，验证程序所有权并清理可再生成内容 |
| `retry_after_storage_cleanup` | 按会话、缓存顺序释放空间，每次删除后重试原批次，成功立即停止 |
