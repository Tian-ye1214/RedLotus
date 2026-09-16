# 普通提交与加急补充

## 行为

- `Enter`：普通提交。当前回合未结束时加入 FIFO 外循环队列，输入显示为浅灰色“排队”消息。
- `Ctrl+Enter`：补充当前内循环，以正常亮度显示“加急”。正在执行的工具继续运行，下一次模型请求同时携带工具结果和补充内容。空闲时直接开始新回合。
- 文字 `/urgent` 入口及其补全、帮助已删除。TUI 通过明确的 `urgent` 参数调用同一会话入口，不向用户正文插入命令前缀。
- 普通询问用 Enter 回答；Ctrl+Enter 仍属于当前任务的加急补充。配置询问始终只消费配置值，不把它送入模型。

普通排队任务和加急消息保持原有的输入边界、附件解析顺序、取消和会话归属规则。

## 此次故障原因

原日志 `写一点_Markdown_用于测试_20260916_213033.log` 的 152–202 行显示：

1. 21:37:59，北京输入登记为 `urgent=False`。
2. 21:38:03，成都补充也登记为 `urgent=False`。
3. 21:38:43，北京调用完成；21:38:44，成都才开始一个新用户回合。
4. 两次 Coordinator invocation 不同，因此确实执行了两个外循环，不能解释为一个回合的两块显示。

原 TUI 没有 Ctrl+Enter 绑定；控制器仅通过文字命令识别加急。此前测试直接发送该命令，漏测了用户实际按键。

## 终端支持边界

输入控件同时接收扩展键盘协议的 `ctrl+enter` 和 LF 对应的 `ctrl+j`。两者都调用相同的加急入口；普通 CR 回车仍然排队。没有新增 Windows 权限库、键盘 Hook 或模型配置。

[Windows Terminal 维护者说明](https://github.com/microsoft/terminal/discussions/17401)：普通 Enter 发送 CR，Ctrl+Enter 发送 LF。扩展协议的 `ESC[13;5u` 也由现有 Textual 解析器支持。

本机 PyCharm 2026.1.2 的 `options/terminal.xml` 配置为 `CLASSIC`。经典 JediTerm 的 [按键编码实现](https://github.com/JetBrains/jediterm/blob/master/core/src/com/jediterm/terminal/TerminalKeyEncoder.java) 在没有专用组合映射时回退到普通 Enter 编码，存在 Ctrl+Enter 与 Enter 无法区分的限制。不能通过把所有普通回车都视为加急来绕过，否则会改变用户要求。

本次未操控 PyCharm 或 Windows Terminal 的物理键盘，也未修改 IDE 设置。验证覆盖了终端字节解析、实际 TUI 分发和真实模型执行；不能据此宣称所有终端版本的物理按键都已通过。若终端丢失修饰键，需要终端本身提供可区分的编码；参考 [Textual 按键说明](https://textual.textualize.io/guide/input/)。

## 验证

证据目录：`WorkDatabase/evaluation/session-refactor-20260916`。

| 验证 | 证据与结果 |
|---|---|
| 修复前失败 | `keyboard-before.txt`：未绑定 Ctrl+Enter，普通排队无浅色区分；`keyboard-windows-before.txt`：LF 未绑定 |
| 输入定向回归 | `keyboard-after-v2.txt`：33 项通过；唯一失败为已知的 core 文件数量超限 |
| 完整本地回归 | `keyboard-full.txt`：312 项通过、1 项既有结构失败，365.51 秒；包括配置输入不进入模型、询问框、停止和会话归属 |
| 真实加急与排队对照 | `keyboard-live-v1`：15 次真实模型请求；加急 1 回合、1 回复，普通提交 2 回合、2 回复；0 WARNING/ERROR |
| 原输入真实复测 | `keyboard-live-original`：原样发送“查询一下北京天气”“还有成都的”，走 LF 加急入口；54.25 秒、13 次真实请求；1 回合、1 回复，包含两座城市；0 WARNING/ERROR |
| 终端显示 | 两个真实目录的 `*-admission.svg` 和 `*-result.svg` 保留 TUI 实际渲染；回归检查排队内容的 dim 样式 |

真实验证使用既有 config.json 的模型、参数和工具；仅隔离存储位置。请求记录、会话、调用轨迹和原始天气查询产物均保留。首组提示词要求对比两城；原输入组没有补写合并回答要求。所有模型响应来自真实服务，按键测试中的 FunctionModel 仅用于独立的辅助回归。

`keyboard-independent-audit.json` 独立检查了 28 份真实请求的工具调用 ID 配对及用户回合数。修改后的 TUI 为 498 个有效代码行，console 为 479 行，system 为 487 行，API base 为 212 行。

core 仍为 10 个 Python 文件的既有结构缺口未在本次输入修复中处理。两种终端的物理按键以及 PyInstaller 程序本轮未实测，不记为通过。
