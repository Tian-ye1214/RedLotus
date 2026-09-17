# 日常环境安装与启动验证

此前的 wheel 验收只覆盖了隔离虚拟环境，没有证明日常 CMD 可以找到 `redlotus`。本次直接使用用户确认的 `D:\Software\miniconda\python.exe`（Python 3.12.13），没有为 RedLotus 新建测试环境，也没有覆盖模型、记忆或配置来源。

## 安装

- 原环境没有安装 RedLotus，这是截图中“不是内部或外部命令”的直接原因。
- 实际用户 PATH 原本已经包含 `D:\Software\miniconda\Scripts`，未修改持久 PATH。最初受限进程读取到的注册表视图不完整，已以宿主用户权限复核并纠正结论。
- 先核实最终 wheel 的应用源码与合并后的源码一致，再安装至上述日常环境。安装退出码为 0。
- 用户明确接受依赖升级后，安装了当前声明的依赖。Gradio、Streamlit 与 Pandas/Pillow、pdf2zh 与 PyMuPDF 的冲突属于已告知并接受的影响；原有 Gradio/Pydantic 冲突仍存在。不能宣称整个 Miniconda 环境 `pip check` 通过。
- 安装缓存、临时文件、版本清单均使用 E 盘。日常安装位于原有 D 盘 Python，不依赖之前的测试虚拟环境。

## 实际 CMD / TUI 操作

通过真实 `cmd.exe` 的终端输入执行，使用持久用户与系统 PATH；不是 `FunctionModel`、模拟 HTTP、Textual headless 或直接调用 Agent 测试驱动。

| 操作 | 结果 |
|---|---|
| 从 `C:\Users\Administrator` 执行 `where redlotus` 和 `redlotus` | 找到 `D:\Software\miniconda\Scripts\redlotus.exe`，进入默认 TUI；未提交用户回合，正常退出码 0 |
| 从实际项目 `E:\代码\Agent` 启动 | 展示真实已有会话的选择器，保留用户原会话 |
| 修复前选择“新建”后立即打字 | 输入框失去焦点，文字没有提交；按 Tab 后才能提交，确认是实际使用缺陷 |
| 修复并重新安装后选择“新建”立即打字 | 无须 Tab，消息正常登记；真实模型回答并实际执行了 Python 环境查询工具，命令返回码均为 0 |
| 执行 `/usage` | 显示项目 `.redlotus/sessions/858294f74c6747fd9148b718e546713c/model_messages.json`，仅一个会话 JSON |
| 正常退出并再次启动，选择恢复 | 恢复同一 session ID 和已保存历史；直接追问前次 Python 版本，真实模型回答 `3.12.13`，本次没有重新运行工具 |
| 恢复后的落盘 | 同一个文件，已完成回合数从 1 增至 2；进程正常退出 |

修复仅在 `RedLotusTui._enter_workspace_after_mount` 完成解锁后，将焦点交回输入框。没有改变模型、RAG、会话计数、记忆范围或存储配置。

本次证明日常 Miniconda 安装、CMD 启动、默认 TUI 输入、真实回复、工具调用、项目内保存和跨进程恢复。未操作 PyCharm / Windows Terminal 的宿主界面，未执行本轮 PyInstaller 验收；不能由本次结果推断这些项目已经通过。
