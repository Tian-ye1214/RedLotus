# 三环境首用错误矩阵

日期：2026-09-18。此报告只覆盖首次配置的缺项、取消和配置文件错误；不替代真实模型、RAG 或业务任务验收。

## 执行方式

所有产品入口均通过真实 Windows ConPTY 启动，没有导入或直接调用 RedLotus 的应用 API。

| 被测环境 | 实际入口 |
| --- | --- |
| 源码 | `E:\代码\Agent\.venv\Scripts\python.exe E:\代码\Agent\main.py` |
| 日常 pip | `cmd.exe /d /c redlotus`，使用 `D:\Software\miniconda` 的已安装入口 |
| Windows onedir | `E:\代码\Agent\WorkDatabase\evaluation\final-three-environment-20260918\installed-onedir-r2\Agent\Agent.exe` |

ConPTY 驱动使用现有项目 `.venv` 中的 `winpty`；它只负责终端控制，不替换被测的 pip 或 EXE 运行时。每个案例的本地配置、全局配置目录、项目目录、临时目录和字节码缓存都位于 E 盘隔离目录。夹具由源码唯一 `src/redlotus/config.json` 的脱敏分发配置生成，只填入不可用的测试值；记录、终端摘录和本报告均不包含真实凭据或正式配置正文。

最终证据根目录：

- `E:\代码\Agent\WorkDatabase\evaluation\final-three-environment-20260918\first-use-errors\three-environment-matrix\matrix-r6`
- `E:\代码\Agent\WorkDatabase\evaluation\final-three-environment-20260918\first-use-errors\three-environment-matrix\matrix-r7`

## 覆盖结果

`✓` 表示退出码、对应字段级提示、配置哈希不变、未创建隔离全局配置、未创建 `model_messages.json` 和驱动无错误均已验证。

| 场景 | 源码 | 日常 pip | onedir |
| --- | --- | --- | --- |
| 空配置后取消 | ✓ `r7/source-empty-config-escape`，97.00 秒 | ✓ `r7/pip-empty-config-escape`，89.88 秒 | ✓ `r7/onedir-empty-config-escape`，15.76 秒 |
| 损坏 JSON | ✓ `r6/source-corrupt-json`，22.20 秒 | ✓ `r7/pip-corrupt-json`，14.51 秒 | ✓ `r7/onedir-corrupt-json`，14.76 秒 |
| `models.coordinator.max_tokens` 类型错误 | ✓ `r7/source-type-error`，22.61 秒 | ✓ `r6/pip-type-error`，19.13 秒 | ✓ `r6/onedir-type-error`，14.75 秒 |
| 缺失 Key、输入空白凭据后取消 | ✓ `r7/source-missing-key-blank-credential-escape`，24.47 秒 | ✓ `r6/pip-blank-key-escape`，79.46 秒 | ✓ `r7/onedir-missing-key-blank-credential-escape`，13.76 秒 |
| 空 `models.coordinator.name` 后取消 | ✓ `r7/source-empty-model-escape`，23.88 秒 | ✓ `r7/pip-empty-model-escape`，21.25 秒 | ✓ `r6/onedir-model-and-policy-escape`，15.83 秒 |
| 缺少 `lifecycle.shutdown_grace_seconds` 后取消 | ✓ `r7/source-missing-policy-escape`，19.43 秒 | ✓ `r7/pip-missing-policy-escape`，15.30 秒 | ✓ `r6/onedir-model-and-policy-escape`，15.83 秒 |

取消案例先让实际字段提示或字段级“无效”提示显示，再发送 Esc；空白凭据、空模型和缺策略字段均没有被静默接受。onedir 的联合案例依次显示缺少策略字段、空模型名和可选 RAG 选择，随后退出而不提交临时填写内容。

首次空配置启动实际耗时为源码 97.00 秒、日常 pip 89.88 秒、onedir 15.76 秒。该表只记录观测值，不将较长耗时归因于特定组件。

每个案例的脱敏终端摘要、动作记录和哈希位于其目录下的 `result.json` 与 `control/actions.json`；汇总为 [r6 summary](../WorkDatabase/evaluation/final-three-environment-20260918/first-use-errors/three-environment-matrix/matrix-r6/summary.json) 和 [r7 summary](../WorkDatabase/evaluation/final-three-environment-20260918/first-use-errors/three-environment-matrix/matrix-r7/summary.json)。

## 范围和限制

这些案例都在配置完成前取消或因验证错误退出，因此不会进入可发送模型请求的状态。没有产生会话文件或模型输出；本批未做网络抓包，因而不能把“未见请求”表述为独立的网络层证明。错误 Key 和不可用模型的真实 API 验收属于后续业务测试。

原始 ConPTY 在完全不输入字节时不会立即绘制 PromptToolkit 的首个输入框。本次取消测试不使用自动 Enter：对需要验证字段的案例先提交一个必然无效的值，等待具体字段的错误提示出现后才发送 Esc。因此它验证了可见错误后的取消和不落盘边界；它不评估终端首次绘制本身的视觉性能。

`matrix-r6` 与 `matrix-r7` 是本报告的最终通过证据。更早的 `matrix-r1` 至 `matrix-r5` 仅保留为驱动调校与启动等待记录，不计入覆盖结果。
