<h1 align="center">RedLotus · 红莲极意</h1>

<p align="center">
  <a href="README.md">English</a> · 简体中文
</p>

<p align="center">
  面向终端的多 Agent 助手，支持任务编排、长期记忆与运行时技能扩展。<br>
  <sub>A terminal AI agent with orchestration, memory, and runtime skills.</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/RedLotus/"><img src="https://img.shields.io/pypi/v/RedLotus" alt="PyPI 版本"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776ab.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-555.svg" alt="Platform">
  <a href="https://ai.pydantic.dev/"><img src="https://img.shields.io/badge/built%20with-Pydantic%20AI-7c3aed.svg" alt="Pydantic AI"></a>
</p>

RedLotus 是一个运行在终端中的 AI Agent。它会根据任务复杂度选择直接处理、委派单个 Worker，或由 Manager 拆解任务并协调多个 Worker 执行。项目兼容 OpenAI 风格的模型接口，并提供记忆、Skills、文件解析、浏览器操作和聊天机器人接入。

```bash
python -m pip install --upgrade redlotus
redlotus
```

## 主要功能

| 功能 | 说明 |
|------|------|
| 多 Agent 编排 | Coordinator 判断处理路径；复杂任务由 Manager 构建带依赖的任务列表，Worker 按依赖分批执行并汇总结果。 |
| 目标模式 | 给定一个明确目标后持续迭代，直到任务完成；执行期间仍可接收用户补充信息。 |
| 三层记忆 | 会话增量记录、每 20 个新增用户回合的 LLM 情景感知、项目情景与全局长期 RAG，以及会话内固定的用户画像与通用经验快照。 |
| 运行时 Skills | 通过 `SKILL.md` 按需加载指令、参考资料和脚本；技能目录会在用户回合开始时重新扫描，无需重启。 |
| 文件与多媒体处理 | 可读取图片，并提取 PDF、Word、Excel、HTML、Markdown、CSV、JSON 和文本文件中的内容。PDF 支持按页提取正文、表格、链接与内嵌图片。 |
| 终端审查与安全护栏 | 提供全屏 TUI、逐块 diff 审查、路径沙箱、危险命令拦截和子进程回收。 |

此外，RedLotus 提供浏览器自动化、图像生成，以及 QQ（NapCat / OneBot）和微信机器人接入。这些能力可按需安装。

## 工作方式

```mermaid
flowchart LR
    U["用户请求"] --> C{"Coordinator"}
    C -->|简单任务| T["直接调用工具"]
    C -->|单步任务| W0["单个 Worker"]
    C -->|复杂任务| M["Manager 规划"]
    M --> D["依赖任务列表"]
    D --> W1["Worker 1"]
    D --> W2["Worker 2"]
    D --> W3["Worker 3"]
    W1 --> R["汇总结果"]
    W2 --> R
    W3 --> R
    MEM["长短期记忆"] -.-> C
    MEM -.-> M
    SK["Skills"] -.-> W0
    SK -.-> W1
```

- 简单请求由 Coordinator 直接调用工具处理。
- 单个独立任务可以委派给一个临时 Worker。
- 复杂请求交给 Manager 拆解；任务会校验重复 ID、未知依赖和依赖环，并按依赖关系分批执行。
- 规划与执行角色可以分别配置模型，在能力、速度与成本之间做取舍。

## 安装

要求 Python 3.12 或更高版本，支持 Windows、Linux 和 macOS。

### 从 PyPI 安装或更新

```bash
python -m pip install --upgrade redlotus
redlotus
```

如果希望将命令安装到独立环境，可以使用 `uv`：

```bash
uv tool install redlotus
```

Windows x64 安装包见 [GitHub Releases](https://github.com/Tian-ye1214/RedLotus/releases)。onedir ZIP 解压后运行 `Agent.exe`，onefile EXE 可直接运行；两者与 pip 入口共用用户配置和项目会话存储，升级不覆盖现有配置与记忆。

面板以会话标题、准确 Token 数及横向条形对比 API 用量。API 用量包含历史重发，单独展示的新增用户输入只计一次。

### 可选能力

```bash
pip install "redlotus[browser]"  # 浏览器自动化
pip install "redlotus[bots]"     # QQ / 微信机器人
pip install "redlotus[viz]"      # 绘图与图像处理
pip install "redlotus[all]"      # 全部可选依赖
```

浏览器能力首次使用前还需要安装 Chromium：

```bash
python -m playwright install chromium
```

## 首次配置

全局配置位于 `~/.redlotus/config.json`。逐字段优先级为本地 `src/redlotus/config.json` → 本地 `.env` → 全局 JSON；源码以仓库根目录、pip 以当前目录、PyInstaller 以 EXE 目录为本地查找基准，不向父目录搜索，也不使用 AppData 配置。`/config` 显示实际来源和修改目标。

可用 `REDLOTUS_CONFIG_FILE` 或 `REDLOTUS_CONFIG_DIR` 显式选择配置。交互启动会根据公开的[字段契约](src/redlotus/config.schema.json)询问必填缺项；非交互启动会列出缺失字段，不从隐藏模板补齐。向导显示来源和写入目标，确认后只写本次修改，取消不落盘。项目会话与日志保存在项目 `.redlotus`，引用、缓存和产物位于项目 `WorkDatabase`；记忆数据库及长期记忆文档位于用户 `.redlotus`。安装包不含凭据。

全新安装可直接运行 `redlotus` 完成配置。输入 `=角色名` 只复用模型名，保留目标角色的连接与策略。`max_context_windows` 只在 JSON 中编辑，缺失或 `null` 时查询 OpenRouter 元数据。以下只是连接字段片段，其他启动字段由向导继续收集：

```json
{
  "BASE_URL": "https://your-api.example.com/v1",
  "API_KEY": "your-api-key"
}
```

网关复用 Pydantic AI 的 OpenAI Chat、OpenAI Responses、Anthropic Messages 和 Google 适配。Manager、Worker、Coordinator、Compressor 可以分别配置或选择命名预设。感知通过 `memory_perception.model_role` 选择子 Agent 配置，与上下文压缩独立。RAG 连接、模型及请求策略由 `SILICONFLOW_BASE`、`SILICONFLOW_KEY`、`RAG_models` 和 `rag_service` 提供。

命名凭据引用（如 `api_key_env`）从同一份三层配置读取，包含本地 `.env`；不读取宿主环境变量或全局 `.env`。直接密钥与命名引用遵守相同的来源优先级。不要将真实密钥提交到 Git。配置与模型路由见 [现行设计](docs/design.md)。

## 终端使用

全屏 TUI 提供三种运行模式，可使用 `Shift+Tab` 循环切换：

| 模式 | 行为 |
|------|------|
| 审查模式 | Agent 写入文件后进入待审查列表，可逐块决定保留或撤销。 |
| 放行模式 | 文件改动直接写入，不进入逐块审查。 |
| 目标模式 | 围绕一个目标持续执行，直到完成或被用户停止。 |

常用快捷键：

| 快捷键 | 作用 |
|--------|------|
| `Enter` | 普通提交；执行中加入外循环队列，以浅灰色“排队”消息显示 |
| `Ctrl+Enter` | 加急补充当前回合，与本批工具结果进入下一次模型请求；空闲时开始新回合 |
| `Shift+Tab` | 切换运行模式 |
| `Ctrl+R` | 打开逐块改动审查 |
| `Ctrl+C` | 停止当前回合 |
| `Ctrl+Q` | 退出 |
| `@路径` | 引用文档、图片或视频，支持 Tab 补全；每次最多 20 个文件 |

补充内容显示为普通用户消息，不添加“加急”标记，终端使用 `Ctrl+Enter` 补充当前回合。如果终端把它与普通回车发送成相同编码，Python 无法区分，需要终端保留组合键信息；`Shift+Tab` 有单独编码，因此仍可能正常工作。按键检测只用于测试，不进入发布界面。具体支持边界与验证记录见 [按键输入说明](docs/design.md#请求执行)。

引用之间不必加空格，例如 `@审稿意见.md解读这个文档，@图片.png分析这张图`。Tab 补全当前引用，遇到含空格或分隔符的路径会自动加引号，也可以手写 `@"路径"`、`@'路径'` 或 `@{路径}`。文件按首次出现顺序去重；超过 20 个不同文件会提示错误，不会只上传其中一部分。

<details>
<summary>常用斜杠命令</summary>

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/clear` | 清空上下文并开启新对话 |
| `/pwd` · `/cd <path>` | 查看或切换工作目录 |
| `/load` | 加载当前工作区的历史会话 |
| `/config` · `/context` · `/panel` | 查看配置、上下文和运行概览 |
| `/skills` | 查看已加载的 Skills |
| `/LTM show` · `/STM show` | 查看长期或短期记忆 |
| `/agent` · `/effort` | 查看或调整角色模型与思考配置 |
| `/api` · `/api embedding` | 配置主模型或向量检索接口 |
| `/compress` | 压缩 Manager / Coordinator 上下文 |
| `/status` · `/trace` · `/tasks` | 查看生命周期、调用追踪和任务状态 |
| `/stop` · `/cancel` | 中断当前回合或 invocation |

</details>

## Skills

Skills 使用目录化的 `SKILL.md` 作为入口。系统只预加载技能名称与简介，在需要时再读取完整说明、参考文件和脚本，减少无关内容对上下文的占用。

项目当前包含以下类型的内置技能：

- 量化回测与 TA-Lib 技术分析
- 浏览器自动化与网页抓取
- FLUX 图像生成与提示词规范
- Agent 编程与 CI/CD 工作流
- 技能创建、管理和安装前审核

也可以在运行期间安装兼容技能：

```bash
npx clawhub --dir skills install <slug>
```

新技能会在后续用户回合自动发现。

## 文件与数据位置

- 会话轨迹和感知任务保存在项目 `.redlotus`，不可变引用快照保存在项目 `WorkDatabase`；压缩只改变模型视图，完整原文保留。
- 项目情景与全局长期记录保存在 LanceDB `memory_records_v3`，按 scope 和项目隔离，通过向量检索与重排召回，服务不可用时保留文本检索。
- `MEMORY.md` 保存用户画像、环境、行为约束与通用经验，不设固定字符上限。它与 system prompt 在会话开始时完整形成快照，写入记忆不重写本会话前缀；新信息通过工具结果和检索消费，新会话读取最新版本。
- 文件和命令工具默认操作当前项目，生成产物保存在 `WorkDatabase/`。`/cd` 先取消旧会话，再切换运行上下文。

Enter 将输入排入 FIFO 队列；Ctrl+Enter 将加急补充加入当前回合，在下一请求边界与本批工具结果一起送给模型。子 Agent 各自拥有线程、事件循环和客户端，遵守配置中的会话线程上限。感知只处理当前会话每 20 个新增用户回合，附带前 3 回合衔接；新建、加载和退出不额外产生短窗口。主动记忆通过 remember 即时处理，失败和取消不会自动成为成功经验。

向量模型、重排、分块、相似度阈值、候选数量和索引参数继续保留。完整的保留项、替代项与迁移行为见 [重构及 RAG 参数说明](docs/design.md)。

## QQ 与微信机器人

安装机器人依赖：

```bash
pip install "redlotus[bots] @ git+https://github.com/Tian-ye1214/RedLotus.git"
```

启动方式：

```bash
python -m redlotus.api.QQ
python -m redlotus.api.WeChat
```

QQ 接入需要先运行 [NapCat](https://github.com/NapNeko/NapCatQQ)，并配置 OneBot WebSocket、机器人 QQ 号和 WebUI token。微信接入在启动后按提示扫码登录。

个人聊天渠道需要配置 `bot.owner_channels.qq`（本人私聊 QQ 号）或 `bot.owner_channels.wechat`（本人 wxid）。未绑定渠道提供文本对话，不开放个人记忆和执行工具。机器人支持 `/stop`、`/clear`；首次启动先收集缺失的渠道运行策略，再连接账号。适配器至真实模型的附件顺序已验证，真实账号收发仍待验收。配置示例见 [渠道绑定](docs/design.md#agent-与工具边界)。

## 本地开发

```bash
git clone https://github.com/Tian-ye1214/RedLotus.git
cd RedLotus

pip install -e ".[dev]"
python main.py
pytest -q
```

构建 Python 包：

```bash
uv build
```

构建 PyInstaller 可执行目录：

```powershell
pip install ".[build]"
$env:PLAYWRIGHT_BROWSERS_PATH="0"
python -m playwright install chromium
pyinstaller build.spec
```

项目主要代码位于 `src/redlotus/`，分为 `runtime`、`sessions`、`core`、`tools`、`memory`、`prompts`、`ui`、`api` 八个模块；命令入口为 `redlotus.api.base:main`。

当前结构整改与发布验收尚未完成。测试必须包含源码、日常 pip 和实际打包入口的真实 API 调用；隔离故障回归不能单独作为通过依据。详见 [开发与验收约定](docs/development.md)。
