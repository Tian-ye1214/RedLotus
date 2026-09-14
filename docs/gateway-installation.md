# 网关、全局数据与安装方式

## 配置归属

`python main.py` 是开发入口，显式选择仓库的 `src/redlotus/config.json` 和开发 `.env`。pip 安装的 `redlotus` 与 PyInstaller 程序默认读取当前操作系统用户的全局配置；启动目录里的 `config.json` 或 `.env` 不会改变它们的配置来源。

Windows 默认目录为 `%LOCALAPPDATA%\RedLotus`；其他平台通过 `platformdirs` 选择用户目录。`REDLOTUS_CONFIG_FILE`、`REDLOTUS_CONFIG_DIR` 和 `REDLOTUS_DOTENV_FILE` 可显式选择配置来源，`REDLOTUS_DATA_DIR` 可隔离测试数据。`/config` 显示实际配置路径。

配置初始化只在启动或显式读取时执行。升级时只从随包 `config.default.json` 补齐缺失项，先备份旧配置，保留用户已经提供的模型、上下文和 RAG 设置。业务组件读取深拷贝；配置编辑通过文件锁和原子写入提交。

全局目录保存配置、核心记忆、LanceDB、引用原件、会话轨迹与感知任务。项目原始事件和会话按 `project_id` 隔离；项目情景只在对应项目召回，全局语义记忆跨本人项目使用。旧 `.redlotus` 数据幂等复制到对应项目目录，保留原件和迁移记录。`WorkDatabase` 继续用于当前项目产物，首次需要写产物时才创建。

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

上下文压缩使用 `compressor`。感知是独立的子 Agent 任务，默认由 `memory_perception.model_role` 选择 `worker`，使用该角色的模型、输出上限与附件限制；其并发由 `memory_perception.max_concurrent` 控制。记忆的窗口、重叠和证据读取预算也由该段配置管理。

会话 system prompt 完整包含通用约束、系统环境、Skills 目录与摘要、角色职责、项目和核心记忆。它在会话内固定；当前时间追加到新输入的运行元数据中。Skills 通过工具逐步读取指令、资源和脚本。摘要带独立的来源标记，即使 SDK 合并消息也能在恢复会话时识别；记忆写入、纠正和清空通过后续结果告知模型，不重写已缓存的系统前缀。

缓存验收使用服务返回的输入命中与未命中 token。主 Agent 与普通任务子 Agent 合计要求大于 90%；压缩和感知单独报告。首次冷请求和压缩后重建上下文的未命中照常计入；缺失用量或取消的调用单独说明，不能把估算值当成真实计量。

## 外部资源与打包

安装程序不向 `site-packages`、可执行程序目录或 PyInstaller 临时解压目录写配置与记忆。随包资源仅包括默认配置模板、提示词、基线 Skills 与必要依赖；凭据、日志和个人记忆不进入分发包。

浏览器功能需要安装 Chromium。旧 Office 转换需要系统 LibreOffice，或通过全局配置/环境中的 `LIBREOFFICE_PATH` 指定 `soffice.com` / `soffice`。脚本使用可发现的外部 Python；冻结程序自身不作为 Python 解释器使用。

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
