# 会话恢复与命令权限修复记录

基线：`codex/cli-state-recovery@421b4c35`。本次只调整已批准的恢复和命令检查行为，不引入系统隔离、Windows 权限库、Hook 或调度框架。

| 问题 | 根因 | 修改及职责 |
|---|---|---|
| 中间缺引号、缺括号后，后续已提交记录可能丢失 | 恢复扫描依赖损坏记录的引号和括号是否闭合 | `SessionFile._recover` 独立识别后续可校验提交；发现后续提交或不符合合法 JSON 前缀时拒绝恢复 |
| 检查与写回混在一起 | 尚未完成全部校验时就可能生成替换文件 | `_inspect` 只返回候选和恢复标记；`_read` 在全部校验通过后才提交候选 |
| 完整尾部批次校验错误被当作半写删除 | 把“最后一批损坏”等同于“最后一批写入中断” | 完整批次的序号/校验失败直接拒绝，原文件字节不变；只恢复可识别的合法半写前缀 |
| 完整 JSON 后的半个 UTF-8 字符被忽略 | 截去末尾未完成字符后未检查它原本是否位于事务内 | 已闭合文档之后的损坏字节拒绝加载；事务字符串内的半字符仍能恢复上一提交 |
| PowerShell 包装隐藏实际命令 | 只检查外层程序；`-Wait` 被当作安全依据 | `_command_invocations` 解析脚本块、条件中的命令、选项缩写；`validate_agent_command` 检查 `Start-Process` 的实际目标与参数 |
| Python 展开、拼接、别名绕过检查 | 静态参数无法直接 `literal_eval` 时跳过 | `PythonCommandCheck.literal` 解析静态列表展开/拼接/变量；`command` 统一检查或拒绝无法确定的启动；显式 executable 和 shell 参数也检查 |
| Node child_process 包装漏检 | 未检查 JS 启动包装 | `JavaScriptCommandCheck` 识别 require、ES import、解构别名、静态参数及 shell 启动，再复用同一命令检查器 |

普通字符串、注释、业务对象的同名方法继续允许。已识别的启动调用无法静态确定程序或命令参数时，要求改成明确命令。这是工具入口检查，不是任意第三方程序的系统级沙箱。

正式配置修改前已备份；本轮配置差异仅有 `execution.permissions` 下的 `require_explicit_commands`、`argv_command_wrappers`、`javascript_modules` 和 `javascript_command_wrappers`。随包模板、开发配置和正式配置同步，模型、RAG、采样、输出、压缩参数未变。

本地证据位于 `E:\代码\Agent\WorkDatabase\evaluation\cache-reuse-repair-20260917`：

- `red.txt`：最初 32 个失败变体；`red-02.txt`：shell/executable/参数列表及正常 Node 写法复现。
- `condition-red.txt`、`condition-green.txt`：条件表达式漏检修复；后者 56 项通过。
- `red-03.txt`、`utf8-red.txt`、`storage-final.txt`：严格恢复口径与 UTF-8 边界；最终 38 项通过。
- `green-02.txt`：命令权限、Skill、普通子 Agent 命令等 83 项通过。
- `full-suite.txt`：完整测试 427 通过、4 失败。两项是既有结构门槛，两项是测试数据库放在 E 盘 exFAT 导致；`rag-ntfs.txt` 在 C 盘隔离测试记忆区复测 5 项通过。
- `live-02/reports/local-regressions.txt`：固定入口本地组 207 通过，仍有两项结构门槛失败。最后的 UTF-8 边界由 `storage-final.txt` 单独覆盖。

两条旧用例要求删除完整但损坏的最后批次，与本轮“仅明确尾部半写才恢复”相反。本轮按新口径验证拒绝修改；原用例完整保存在 `session-tests-before-strict-recovery.py` 和 `storage-tests-before-strict-recovery.py`。另外保留“嵌套/字符串中的伪事务不能成为正式提交”的真实半写用例，未将其删除。

固定验收入口：

```powershell
python scripts/cli_state_acceptance.py --config <正式配置路径> --dotenv <显式凭据文件> --root <E盘评测目录> --dependencies <测试依赖目录> --regressions --deadline 600
```

`--regressions` 的本地测试与真实请求共享 600 秒截止时间。结构失败不隐藏，也不因其他行为通过而标记整体通过。CLI、输入中断、存储失败、diff/审查/goal 等已有断言继续保留。
