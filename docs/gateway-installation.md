# 网关、全局数据与安装方式

## 配置归属

pip 安装后运行 `redlotus`，全局基准配置为 `~/.redlotus/config.json`。逐字段读取顺序统一为：本地 `src/redlotus/config.json` → 本地 `.env` → 全局 JSON。源码以仓库根目录为本地目录，pip 以当前目录为本地目录，PyInstaller 以 EXE 目录为本地目录；不搜索父目录，不把临时解压目录作为配置来源。

各平台均使用当前用户的 `.redlotus` 目录。`REDLOTUS_CONFIG_FILE`、`REDLOTUS_DOTENV_FILE` 和 `REDLOTUS_CONFIG_DIR` 可为隔离测试显式选择三层来源，`REDLOTUS_DATA_DIR` 隔离状态。`/config` 显示读取顺序和修改目标。`AppData\Local\RedLotus` 不再作为默认目录，也没有旧目录迁移逻辑。

不再提供第二份默认模板或启动补值。必填配置缺失时报告字段和来源；JSON 损坏或类型错误不能靠下一层隐藏。业务组件读取 `copy.deepcopy`，配置命令通过文件锁和原子写入只保存用户修改，不把合并结果整份落盘。有本地 JSON 时修改本地，否则修改全局。新安装缺少全局配置时，应先提供完整配置；程序不会选择隐藏默认模型。

`.env` 支持所有字段，嵌套键使用与 JSON 一致的名字及双下划线，例如 `models__worker__max_tokens=393216`。数组和对象使用 JSON 值，普通文本保持字符串；不展开宿主环境变量。空白连接配置继续查找下一层，`false`、`0`、空列表及允许的 `null` 不会被当成缺失。全局 `.env` 不是第四层来源。

全局记忆、LanceDB 和日志默认保存在 `.redlotus`；项目情景按 `project_id` 隔离。会话、引用、运行环境继续遵守显式存储配置，`WorkDatabase` 保存项目产物。打包时不携带开发配置或 `.env`；EXE 旁可自行放置外置开发配置。

## 命名网关与模型预设

以下是配置结构示例，地址、凭据引用和模型名称应填写服务实际提供的值：

```json
{
  "gateways": {
    "primary": {
      "protocol": "openai-chat",
      "base_url": "https://gateway.example/v1",
      "api_key_env": "PRIMARY_MODEL_KEY"
    },
    "messages": {
      "protocol": "anthropic",
      "base_url": "https://messages.example",
      "api_key_env": "MESSAGES_MODEL_KEY"
    }
  },
  "model_presets": {
    "project-model": {
      "gateway": "primary",
      "name": "configured-model-name",
      "settings": {"temperature": 1, "max_tokens": 393216},
      "input_limits": {"max_file_bytes": 20000000}
    }
  },
  "models": {"coordinator": {"preset": "project-model"}}
}
```

可用协议是 `openai-chat`、`openai-responses`、`anthropic`、`google`，旧 `openai` 名称仍可读取。协议由配置指定，不根据模型名称选择。直接在 `models.<role>` 写名称和参数的原有格式继续可用。

`/agent <role> <预设或模型名称>` 支持补全与运行中选择。新目标在当前模型请求及本批工具结束后的下一次请求生效；已创建的子 Agent 保留自己的配置快照。任务、工具结果、会话和引用继续保留；目标上下文较小时先走压缩流程。状态区区分请求使用的配置模型与服务返回的模型标识，避免把服务端别名误当作另一份本地配置。

## 参数来源与提示词缓存

模型请求参数在 `models` 或 `model_presets` 中设置；通用网关设置在 `model_gateway`，连接超时使用 `MODEL_HTTP_TIMEOUT` 与网关的可选覆盖。RAG 模型名称、连接、批量与检索设置分别由 `RAG_models`、`rag_service`、`short_term_memory` 和 `long_term_memory` 提供。工厂把复制后的配置交给 SDK，协议编码不修改用户的采样和输出预算。

上下文压缩使用 `compressor`。感知是独立的子 Agent 任务，由 `memory_perception.model_role` 选择角色，使用该角色的模型、输出上限与附件限制；同一会话的 Agent 工厂线程共享 `agent_run_policy.max_concurrent_threads_per_session` 上限。记忆的窗口、重叠和证据读取预算由 `memory_perception` 管理。

会话 system prompt 完整包含通用约束、系统环境、Skills 目录与摘要、角色职责、项目和核心记忆。它在会话内固定；当前时间追加到新输入的运行元数据中。Skills 通过工具逐步读取指令、资源和脚本。摘要带独立的来源标记，即使 SDK 合并消息也能在恢复会话时识别；记忆写入、纠正和清空通过后续结果告知模型，不重写已缓存的系统前缀。

缓存验收使用服务返回的输入命中与未命中 token。主 Agent 与普通任务子 Agent 合计要求大于 90%；压缩和感知单独报告。首次冷请求和压缩后重建上下文的未命中照常计入；缺失用量或取消的调用单独说明，不能把估算值当成真实计量。

## 外部资源与打包

安装程序不向 `site-packages` 或 PyInstaller 临时解压目录写配置与记忆。随包资源仅包括提示词、基线 Skills、渠道配置示例与必要依赖；开发 JSON、`.env`、凭据、日志和个人记忆不进入分发包。EXE 旁已有的外置开发 JSON 可由配置命令修改。

浏览器功能需要安装 Chromium。旧 Office 转换需要系统 LibreOffice，或通过三层配置中的 `LIBREOFFICE_PATH` 指定 `soffice.com` / `soffice`。脚本使用可发现的外部 Python；冻结程序自身不作为 Python 解释器使用。

```powershell
# 构建独立目录
./scripts/pack.ps1
# 构建单文件
./scripts/pack.ps1 -OneFile
```

两种模式分别输出到 `dist/onedir`、`dist/onefile`。验收结束后可以删除生成的 `build`、`dist`；全局配置和记忆不在这些目录中。

实现复用 [Pydantic AI 模型适配](https://pydantic.dev/docs/ai/models/overview/) 与 [消息历史](https://pydantic.dev/docs/ai/core-concepts/message-history/)，打包路径遵循 [PyInstaller 运行时说明](https://pyinstaller.org/en/stable/runtime-information.html)。真实覆盖范围以对应版本的验收报告为准，辅助协议测试不代表已访问其他服务。

## 复现实测

真实验收读取当前全局配置并保存配置指纹，不修改模型参数。测试数据通过 `REDLOTUS_DATA_DIR` 隔离；所有请求仍访问真实服务，会产生实际用量。准备浏览器、LibreOffice 以及脚本执行用的外部 Python 后，可以从仓库外使用干净环境中安装的 wheel 运行以下脚本。

`scripts/real_acceptance.py --root <新的测试目录>` 覆盖六类场景；`scripts/real_soak.py --root <新的测试目录>` 连续运行至少两小时、200 个用户回合。测试依赖包含开发 extra、浏览器 extra 和 `psutil`。Windows 原生 CLI/TUI 验收脚本 `scripts/installed_entry_acceptance.py` 另需 `pywinpty`、`pyte`，并通过 `--executable` 指定安装后的入口或冻结程序。

报告中的缓存比例使用服务返回的缓存命中 token 除以全部输入 token，包含首次冷请求；没有返回缓存用量的协议显示为未测量。运行阶段的非预期 WARNING/ERROR 或任务资源未释放应判为失败。用户退出时允许取消尚未完成的记忆生产，但必须保留可恢复窗口、不误推进完成位置，并验证下次启动可以继续处理。测试完成后可以保留报告、请求统计和终端证据，删除生成的打包目录。
