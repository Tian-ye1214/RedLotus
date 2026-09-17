# 面板趋势、Agent 状态及 Token 统计验收

日期：2026-09-17。基线：`develop@3d05a300`。实施分支：`codex/panel-statistics-fix`。

## 修复与口径

| 问题 | 修改 | 验证结果 |
|---|---|---|
| 一个会话使趋势图填满蓝色 | 零点、单点、全零、等值使用文字；多个不同值才绘图。标题明确为 API 累计用量 | 辅助测试覆盖五种输入；真实终端验证空项目、单会话、两会话 |
| 没有计划清单时 Running 为 0 | 主回合状态加工厂实际存活线程；等待名额单列 Queued；排除感知、标题、压缩和其他会话 | 真实对话 Running 1，实际 Worker 协作 Running 2，停止或完成后为 0；排队与读取失败由辅助测试验证 |
| 历史重发被混作新增 Input | 在输入被当前回合消费时按身份记账；同会话引用哈希去重；累计计数进入原会话事务并随精简保留 | 真实 Input 在工具往返中保持不变；加急只增加一次；恢复不重算；API 累计输入另列 |

主图显示当前项目所有会话的新增内容。用户输入是现有算法估算值，模型生成来自唯一响应的 usage。推理从总输出拆分，不重复相加。未知推理、缺失 usage、旧会话缺少输入账本或未计量附件都会明确标注；不展示虚假的完整比例。趋势图及 `/usage` 仍为服务端 API 口径。

已有任务清单继续使用原任务统计，改名为“计划任务进度”；无清单显示“暂无计划任务”。刷新周期仍为三秒。

## 函数职责

| 入口 | 单一职责 |
|---|---|
| `SessionFile.record_input / input_usage` | 保存输入身份、引用哈希及计数；读取累计值和覆盖状态 |
| `AgentSystem.record_user_input` | 将已消费输入接入原有可重试事务入口；覆盖普通、加急和工具询问回答 |
| `SubagentFactory.activity` | 只读区分当前会话前台子 Agent 的运行与等待 |
| `ContentTokenStats / summarize_messages` | 聚合新增内容及已报告输出，保存缺失信息 |
| `_collect_runtime / _collect_history` | 分别收集运行状态与历史统计 |
| `_update_token_trend / _update_content_chart / _update_api_usage / _update_agent_counts` | 分别呈现趋势、新增内容、实际 API 用量和 Agent 状态 |

未新增统计文件、正文副本或调度器。原会话压缩快照增加 `inputs` 计数。旧会话保持可读，不猜测补写历史。面板异步刷新提交前检查项目和 session，防止切换后显示旧状态。

## 辅助回归

修复前新增测试得到 9 项失败，复现旧趋势及缺失的输入、Agent 状态能力。最终同一源码执行：

```powershell
D:\Software\miniconda\python.exe -m pytest tests/test_panel_statistics.py tests/test_session_statistics.py tests/test_session_file.py tests/test_session_transactions.py tests/test_cli_storage_transactions.py tests/test_session_thread_limit.py tests/test_system.py tests/test_cli_transition_recovery.py -q --tb=short -p no:cacheprovider --basetemp=WorkDatabase/runtime/panel-statistics-regression
```

结果：**101 passed in 74.13s**。覆盖输入去重、独立同文提交、引用版本变化、加急消费时点、未消费输入、工具询问回答、存储精简与恢复、缺失数据、实际线程名额及已有事务和切换回归。这是定向辅助测试，不等同于完整项目验收。

## 日常环境真实验证

没有创建 RedLotus 虚拟环境。使用现有 `D:\Software\miniconda\python.exe` 安装修订版，`--no-deps --no-build-isolation --no-cache-dir --force-reinstall`，依赖版本不变。六个变更源码文件与 `D:\Software\miniconda\Lib\site-packages\redlotus\core` 对应文件逐一核对一致。

从 E 盘临时项目使用日常 PATH，经 `C:\Windows\System32\cmd.exe /d /c redlotus` 启动真实 TUI；使用现有全局配置和服务，实际模型报告为 `deepseek-flash`。安装与回归分批；交互验证含重启约 **551 秒**，未超过 600 秒。

| 操作 | 独立核验 |
|---|---|
| 空项目打开 `/panel` | 无趋势图；Running 0；暂无计划任务 |
| 委派创建、读取中文文本并核对字数 | 实际文件为“晚风经过松林”，六个汉字；真实命令返回；执行时 Running 1 |
| 单会话查看面板 | 显示具体 tokens 及“仅一个会话，暂无趋势”，不绘制实心块 |
| 实际 Worker 等待并读取文件 | 有 Worker 工具轨迹；面板 Running 2、Queued 0；完成后归零 |
| Ctrl+Enter 加急 | CSI-u 按键序列真实提交；同一 ID 登记与消费各一次，Input 230 → 252；没有多计真实用户回合 |
| 运行命令中 `/stop` | 实际取消回执；下一次面板刷新 Running 0；已消费 Input 保留 |
| 正常退出、CMD 重启并选择恢复 | 原 session ID、原文件保持不变；Input 279 → 279；恢复本身不增加回合 |
| 一条输入重复引用同一个文本 | 模型实际识别六个汉字；引用哈希仅一条，计 6 个估算 Token |
| `/clear` 后真实问候、再次打开面板 | 新 session 从零记账；两个不同值按旧到新绘图；Agent 无残留 |

加急证据：`4398448981c8491cba79e4276bc9fd97` 于 21:44:47 登记、21:45:20 消费，均属于 `8fc919689df34514bca423953d66d4e6` 回合。

最终会话 `362e2236936f4af89eb12a706ccfd69f` 完成 6 回合，新增输入估算 329；新会话 `cd3c6caa2c6749e296b786a74719085b` 完成 1 回合，新增输入估算 10。各一个 `model_messages.json`，未达 20 回合，均没有感知任务。

| 最终统计 | 值 |
|---|---:|
| 新增用户输入（估算） | 339 |
| API 实际输入 | 191,753 |
| 已报告输出总量（含推理） | 17,588 |
| 已报告推理 | 15,051 |
| 已报告非推理输出 | 2,537 |
| API 输入缓存命中 / 未命中 | 170,752 / 21,001 |
| API 缓存命中率 | 89.05% |

20 条响应记录中 19 条有服务端用量；一条 Worker 轨迹记录没有 model_name、usage 和推理明细。因此真实面板标注统计不完整，不显示完整占比；上表已报告数不代表补齐了未知值。服务端已报告的非推理输出与推理之和为已报告总输出。缓存口径未修改，此批未达到历史的 90% 目标，不把面板修复称为缓存改进。

项目应用日志 **0 WARNING / 0 ERROR**；两个实际 CMD 进程正常退出，退出后未发现本轮 RedLotus 或测试命令残留。

## 边界与清理

- 首次自动终端发送将任务和 `/panel` 连写在同一批键盘字节中，产生了两次独立接纳；后续使用逐条提交。账本按真实接纳身份分别计数，不人为删掉这部分 usage。该现象可能涉及驱动或粘贴处理，本轮不宣称修复快速粘贴输入。LF 在这条 ConPTY 路径被当成普通 Enter；Ctrl+Enter 使用实际 CSI-u 序列验证。
- 线程排队、未知明细、旧会话覆盖不足和存储精简使用辅助用例；实际安装验证覆盖主 Agent、Worker、停止、加急、恢复和重复引用，没有用辅助用例冒充模型请求。
- 未重做 PyInstaller、Windows Terminal、PyCharm 交互或整个项目全部功能验收；既有源码结构超标不在本轮范围。
- `pyproject.toml` 多余的 `r` 在本轮开始前已经删除，未调整依赖。源码配置 SHA256：`28341cf1f203148d5752a1117a306d07a44f7aeb04c40ebfd33d47364d1ef86d`；全局配置 SHA256：`3ad77345d5ea65dffc185074d8b04230bb1d32d5f4bc4c6ede9fa8b4b8991751`。本轮均未改写。

本轮临时数据清理集合（相对仓库根目录，删除前检查绝对边界及进程）：

| 目录 | 文件字节数 |
|---|---:|
| `WorkDatabase/runtime/panel-statistics-validation` | 7,587,300 |
| `WorkDatabase/runtime/panel-statistics-regression` | 1,345,004 |
| `WorkDatabase/runtime/panel-statistics-tests-reply` | 14,197 |
| `WorkDatabase/runtime/pytest-5286271f0f6a45f681b41d17b5c94385` | 0 |
| `WorkDatabase/runtime/pytest-c92f6bcf967f4995a183594ee31d9c01` | 117,956 |
| `WorkDatabase/runtime/pytest-9093665019f542afb50267446313f2e8` | 170,570 |
| `WorkDatabase/runtime/pytest-53d7ab6a08ca4fc7b1d506bf8803d33f` | 878,158 |
| `build`（本轮 pip 构建产生） | 1,117,775 |

清理只涉及上述本轮测试会话、夹具、安装临时文件和构建目录；正式会话、配置、记忆、已有项目产物及旧工作树不在范围内。安装到日常环境的修订版保留。

清理已执行，八个路径逐一复查均不存在，共移除 11,230,960 字节的文件内容；本报告和回归用例保留。
