# RedLotus 1.0.1 发布验收

## 范围与环境

本版将独立 Sparkline 替换为历史会话表中的 API 用量对比：时间、标题、Agent、响应次数、完整 Token 数和相对条形。缺失用量明确标注，不用条形暗示完整统计。新增内容计数、API 用量、Agent 状态和计划任务口径不变。

基线为 `develop@bf47355a`，开发分支为 `codex/release-1.0.1`。使用用户日常环境 `D:\Software\miniconda\python.exe`（Python 3.12.13），没有新建 RedLotus 测试虚拟环境。构建工具为 PyInstaller 6.22.3、build 1.6.1、setuptools 70.2.0、wheel 0.46.3。仅安装缺少的构建工具；应用依赖声明和锁定版本未修改。

源码配置 SHA-256：`28341cf1f203148d5752a1117a306d07a44f7aeb04c40ebfd33d47364d1ef86d`。
全局配置 SHA-256：`3ad77345d5ea65dffc185074d8b04230bb1d32d5f4bc4c6ede9fa8b4b8991751`。
两者与实施前一致，模型、RAG、压缩参数和提示词未修改。

## 本地回归

- 新增的展示用例先在旧实现上得到 10 项失败，修改后通过。
- 面板、会话统计、事务保存、输入切换、线程及导入副作用等定向回归：**140 passed，131.42 秒**。
- 包资源及打包约束回归：**6 passed，3.50 秒**。
- 覆盖无会话、单会话、多会话、全零、等值、极大差异、长中文标题、缺失用量，以及 60／80／120／220 列渲染。
- 原安装测试为排除旧源码，错误地移除了整个包含 RedLotus 的 site-packages，导致 22 项依赖缺失误报。修复测试的源码定位方式，保留原有断言，复跑同一批测试通过。此项属于测试脚手架问题，不冒充应用缺陷修复。

## 日常环境真实运行

使用实际终端输入、已配置模型服务和真实工具，未伪造响应或向量。测试项目位于 E 盘中文及空格路径；源代码未加入该项目的 Python 搜索路径。

| 批次 | 实际操作 | 结果与耗时 |
| --- | --- | --- |
| 已安装 wheel / CMD `redlotus` | 对话、委派 Worker 写文件、主 Agent 独立读取、运行中查看面板、`/stop`、退出 | exit 0；168.88 秒 |
| CMD 重启恢复 | 启动选择原会话，继续问之前的校验词 | 正确回复 `银杏731`；原 session ID；exit 0；69.84 秒 |
| onedir `Agent.exe` | 加载原会话、读取产物、`search_episodes`、面板、新会话及真实对话、退出 | exit 0；135.61 秒 |
| onefile `Agent.exe` | 加载原会话、真实追问、两个会话的用量表、退出 | 正确回复 `银杏731`；exit 0；208.02 秒，包含解压和退出清理 |

真实执行中观察到主 Agent Running 1，委派 Worker 期间 Running 2，结束及停止后 Running 0／Queued 0。无计划任务时显示“暂无计划任务”。产物 `release-check.txt` 的内容由验收端独立读回，确认为 `银杏731\n`。

真实会话文件核对结果：

| 会话 | 用户回合／输入身份数 | 新增输入估算 | 已记录响应数 | API 输入 | API 输出 | 表中总数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 发布验收 | 6／6 | 253 | 17 | 115,356 | 3,421 | 118,777（已报告部分） |
| 简短计算 | 1／1 | 34 | 1 | 7,350 | 8 | 7,358 |

同一轮工具往返没有增加用户输入身份数；多次恢复继续写原文件，没有复制会话或把旧回复计成新输入。面板数值与持久化 usage 的输入加输出一致。该样本存在 2 条缺失用量／推理明细的响应，界面如实标注不完整，不推测为零。

测试项目日志中 **0 条 WARNING／ERROR**。此结论只对应上述真实批次；不代表所有协议、所有功能和长期运行均已重新验收。

## Windows 构建与包检查

首次 onedir 构建在 600 秒超时，未作为发布产物。依赖图确认额外收集来自 LanceDB 可选本地 tensor／transformer 后端、Google SDK 的 Notebook 展示以及未启用的 Hugging Face 网关；它们又带入日常环境中的 Gradio 等软件。只在打包清单排除 `IPython`、`torch`、`transformers`、`huggingface_hub`，没有移除 RedLotus 的已用协议、工具或 HTTP embedding。外部 Skill 解释器不受此清单影响。

修正后 onedir 构建 exit 0，用时 **253.72 秒**。onefile 完整构建日志在 **320.729 秒**报告 Build complete，生成文件随后完成真实运行；原构建工具会话在用户中断后不可恢复，因此不虚构该调用的外层退出码。两种构建均在 600 秒期限内生成最终产物。

审计 wheel、sdist 以及 onedir 的 3,331 个文件：未包含开发 `config.json`、`.env`、真实凭据、会话、个人记忆、运行日志或测试项目；提示词、Skills、文档解析依赖及 LanceDB 原生 `_lancedb.pyd` 保留。归档和审计批次耗时 46.03 秒。

onedir 未压缩约 755 MiB，LanceDB 原生库约 296 MiB。onefile 会在项目 `WorkDatabase/runtime/pyinstaller` 解压，当前 E 盘日常环境下启动明显慢于 onedir。经常使用建议选择 onedir 或 pip；没有通过移除既有能力缩小安装包。

| 产物 | 字节 | SHA-256 |
| --- | ---: | --- |
| redlotus-1.0.1-py3-none-any.whl | 376,321 | `57b161b3d24eeb43d50eaffd00275f247c3e8a8efad3427ea7df594d130371e8` |
| redlotus-1.0.1.tar.gz | 428,322 | `38647b878493cb6b732c7dba5edbd7bc8a91ffd9e93c657ff1ae8649c95b67a3` |
| RedLotus-1.0.1-windows-x64-onedir.zip | 320,502,097 | `1ad68eef936ada61eff4f42b0da47d7cdef2906dfcfa6ea417941630327543ee` |
| RedLotus-1.0.1-windows-x64.exe | 302,997,726 | `25b8478b86d908d12172175e855facd5c44bc6213436c5923366276233379182` |

## 发布与清理

- 已合并并推送 `develop@765ee50b`；`v1.0.1` 指向完整提交 `765ee50b3daf863b532fdb04d61faf35861b2841`。
- [PyPI 1.0.1](https://pypi.org/project/RedLotus/1.0.1/) 已发布 wheel、sdist，公开 JSON 中的哈希逐项与本地一致。
- [GitHub v1.0.1](https://github.com/Tian-ye1214/RedLotus/releases/tag/v1.0.1) 已公开且设为最新正式版。两份 Windows 包、说明和 SHA256SUMS 均完成服务端哈希核对。
- GitHub 首次草稿请求因缩写 target_commitish 被拒绝，改用完整标签提交后成功；未改为从默认 master 发布。
- PyPI 接收 sdist 后拒绝旧构建工具生成的大写 wheel 文件名和内部 dist-info 路径。使用标准 `wheel pack` 规范化路径并重建 RECORD，逐字节确认其他应用、资源和元数据内容不变；未重复上传已接受的 sdist。新 wheel 在日常环境重装、启动及加载通过，exit 0，25.88 秒，再补传成功。上表为最终发布哈希，原候选 wheel 哈希 `04baacbc953d8768b0a35a463de9684f471a09632cb26f2c00d3e065fa640f81` 未作为公开 wheel 发布。
- 从正式 PyPI 执行强制升级：本次 pip 选取 sdist，经标准构建后安装到日常 Miniconda。通过 CMD `redlotus` 启动、恢复原会话并真实追问，正确回复校验词，exit 0，104.78 秒。没有将 pip 的临时构建目录作为额外 RedLotus 测试环境。
- GitHub 重新下载耗时 100.58 秒，四份文件与本地校验一致；onedir 解压耗时 36.11 秒。首次下载尚未结束时的提前哈希检查被拒绝，等待下载进程 exit 0 后重新检查通过，未将未完成下载当作产物损坏。

- 重新下载的 onedir 实际启动、恢复 21 条 SDK 消息并正常退出，exit 0，89.53 秒；下载的 onefile 同样恢复原会话并正常退出，exit 0，139.39 秒。它们与发布前测试的二进制哈希一致。

## 清理记录

- 已删除本地 `codex/release-1.0.1`。远端查询无 `codex/*` 分支；本次开发分支未推送到远端。
- `git worktree list --porcelain` 仅剩主工作区。旧 `project-storage-sqlite` 与不存在的 `session-refactor-worktree` 不再是有效 worktree。
- 旧 worktree 的可读残留文件已逐项删除 **51,188 个**。整树删除会因一处既有文件系统损坏提前停止，因此改为同一边界内逐文件删除，没有扩大目标目录。
- **未能完全删除的旧缓存**：`E:\代码\Agent\WorkDatabase\worktrees\project-storage-sqlite\WorkDatabase\runtime\packaging\uv-cache\archive-v0\XtU2bKtESdhjJfuK\redlotus-1.0.0.dist-info`。Windows 枚举返回“文件或目录损坏且无法读取”，删除返回“目录不是空的”；这个节点及祖先目录仍保留。未执行整卷修复或改变磁盘文件系统。

- 本轮临时根目录 `E:\代码\Agent\WorkDatabase\runtime\release-1.0.1` 已删除；清理前包含 8,155 个文件、约 3.47 GiB，涵盖构建产物、重复下载、测试项目、缓存及工具。生成的 `src/RedLotus.egg-info` 也已删除；复查仓库 `build`、`dist` 均不存在。
- `.git/worktrees/project-storage-sqlite` 和 `.git/worktrees/session-refactor-worktree` 的残留管理目录仍可被 `Test-Path` 发现，此前清理返回访问拒绝；它们已不出现在 Git 的有效 worktree 列表中。与上述损坏缓存一并保留为未完全清除项，不宣称磁盘上已无任何旧 worktree 残留。

正式会话、配置、记忆和项目产物不在清理范围。日常 Miniconda 中已升级的 RedLotus 1.0.1 保留，发布标签保持指向经过构建和验收的应用提交；此后的文档提交只补记发布复核与清理结果。
