# 普通提交与加急补充

## 行为

- `Enter`：普通提交。当前回合未结束时加入 FIFO 外循环队列，输入显示为浅灰色“排队”消息。
- `Ctrl+Enter`：补充当前内循环，按普通用户消息显示，不增加标签或登记说明。正在执行的工具继续运行，下一次模型请求同时携带工具结果和补充内容。空闲时直接开始新回合。
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

启动现在先提供可跳过的按键检测，输入栏旁的“按键检测”按钮可随时重测。检测依次接收普通 Enter 和 Ctrl+Enter，按实际事件显示“已验证支持”“终端未传递区别”或“尚未验证”，不根据终端名称判定。检测与聊天输入隔离，保留原草稿；取消不创建会话、不发模型请求、不计用量，也不持续记录按键。配置预览只供手动审查，不自动写入终端设置。

输入控件仅将 `ctrl+enter` 作为加急，`enter` 作为普通排队输入。LF 不作为加急输入，也不提供替代快捷键。没有新增 Windows 权限库、键盘 Hook 或模型配置。

[Windows Terminal 维护者说明](https://github.com/microsoft/terminal/discussions/17401)：普通 Enter 发送 CR，Ctrl+Enter 发送 LF。扩展协议的 `ESC[13;5u` 也由现有 Textual 解析器支持。

本机 PyCharm 2026.1.2 的 `options/terminal.xml` 配置为 `CLASSIC`。经典 JediTerm 的 [按键编码实现](https://github.com/JetBrains/jediterm/blob/master/core/src/com/jediterm/terminal/TerminalKeyEncoder.java) 在没有专用组合映射时回退到普通 Enter 编码，存在 Ctrl+Enter 与 Enter 无法区分的限制。不能通过把所有普通回车都视为加急来绕过，否则会改变用户要求。

2026-09-20 的用户实测确认：PyCharm Classic 仍显示排队，Windows Terminal 在下述已批准配置后正常进入内循环。15:33:59 的 PyCharm 日志为 `input key=enter`；15:45:49 的 Windows Terminal 日志为 `input key=ctrl+enter`，补充登记为当前回合的 `urgent=True`。物理按键由用户操作，自动验收没有伪装成物理按键测试。

Windows Terminal 1.24.11911.0 经用户查看差异并批准后，仅新增 `User.RedLotusCtrlEnter` 动作（`sendInput` 发送 `\u001b[13;5u`）及 `ctrl+enter` 绑定；回读确认其他设置未变。普通 Enter、Ctrl+J、PyCharm 设置和 RedLotus 配置未修改。此绑定影响 Windows Terminal 内的其他程序；不作为应用启动时的隐式设置。

PyCharm Classic 仍未通过。重复增加 Textual 绑定不能恢复终端已丢失的修饰键。用户已决定暂不增加 PyCharm 插件，因此本轮保留这一限制和现有按钮入口。未安装 IDE 插件，也未增加 Windows API、系统 Hook 或替代快捷键。参考 [Textual 按键说明](https://textual.textualize.io/guide/input/)。

## 历史验证（不作为当前按键规则的验收）

最新启动检测由用户实际按键复核：Windows Terminal 显示通过，PyCharm 显示不通过。检测结果只说明当前终端传入的按键事件，不会自动修改终端设置；新环境需重新检测。

证据目录：`WorkDatabase/evaluation/session-refactor-20260916`。

| 验证 | 证据与结果 |
|---|---|
| 修复前失败 | `keyboard-before.txt`：未绑定 Ctrl+Enter，普通排队无浅色区分；`keyboard-windows-before.txt`：LF 未绑定 |
| 输入定向回归 | `keyboard-after-v2.txt`：33 项通过；唯一失败为已知的 core 文件数量超限 |
| 完整本地回归 | `keyboard-full.txt`：312 项通过、1 项既有结构失败，365.51 秒；包括配置输入不进入模型、询问框、停止和会话归属 |
| 真实加急与排队对照 | `keyboard-live-v1`：15 次真实模型请求；加急 1 回合、1 回复，普通提交 2 回合、2 回复；0 WARNING/ERROR |
| 原输入真实复测 | `keyboard-live-original`：原样发送“查询一下北京天气”“还有成都的”，走 LF 加急入口；54.25 秒、13 次真实请求；1 回合、1 回复，包含两座城市；0 WARNING/ERROR |
| 终端显示 | 两个真实目录的 `*-admission.svg` 和 `*-result.svg` 保留 TUI 实际渲染；回归检查排队内容的 dim 样式 |

以下是当时的验证记录；其中 LF 加急行为现已删除，不能作为当前实现的通过依据。真实验证使用既有 config.json 的模型、参数和工具；仅隔离存储位置。首组提示词要求对比两城；原输入组没有补写合并回答要求。所有模型响应来自真实服务，按键测试中的 FunctionModel 仅用于独立的辅助回归。

`keyboard-independent-audit.json` 独立检查了 28 份真实请求的工具调用 ID 配对及用户回合数。修改后的 TUI 为 498 个有效代码行，console 为 479 行，system 为 487 行，API base 为 212 行。

以上历史批次未覆盖物理按键和 PyInstaller；不得与上面的最新用户实测混算。core 文件数量的既有结构缺口未在本次输入修复中处理。
