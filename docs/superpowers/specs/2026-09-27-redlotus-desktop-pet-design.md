# RedLotus 桌宠：GitHub 调研与首版功能设计

日期：2026-09-27。状态：用户已确认首版实现方案；本文保留 GitHub 调研快照，并描述当前实现与验收契约。

角色资产以用户提供的两张基础像素模板为准，分别保留深色外套与米色针织衫角色的发型、眼镜、服装和比例。桌宠保留已转换完成的运行时精灵图与来源说明，制作目录和转换脚本已移除。

调研阶段已完成成熟项目筛选、参考或复用价值判断及功能接入设计。用户确认：实现成本优先，倾向操作系统桌面悬浮；初版只有像素宠物和鼠标动作互动，不承担 Agent 功能。当前动作资源位于 [src/redlotus/static/pets](../../../src/redlotus/static/pets)，素材来源见 [ASSET-NOTICE.md](../../../src/redlotus/static/pets/ASSET-NOTICE.md)。

## 1. 推荐结论

**推荐 Python + PySide6 独立桌宠进程，由现有 CLI/TUI 共用的控制器启停。主要参考 VS Code Pets 的角色继承与动作切换、oneko.js 的小型精灵动画。**

没有发现一个同时满足“高 star、Python、简单像素宠物、可直接并入当前 MIT 项目、素材授权清楚”的现成完整方案。这里推荐的是复用成熟库、参考成熟项目中的小范围设计；整套 fork 的接入和裁剪成本更高。

- **最值得读源码：VS Code Pets。** 像素风格、鼠标互动和 BasePetType → Cat 继承结构接近需求。
- **最值得参考小型行为逻辑：oneko.js。** 实现小，适合研究待机、睡眠、精灵帧及动作恢复。
- **桌面窗口参考：BongoCat。** 关注透明、无边框、置顶和平台差异。
- **Python 同栈参考：DyberPet。** 可研究 Qt 鼠标事件；GPL 和源码/发行版本差异使直接移植不适合作为默认方案。
- 原创首版角色素材。仓库代码开源，不代表角色图片可以一起复制、修改和分发。

上述选择是根据代码审阅和 RedLotus 结构作出的工程判断，尚不是性能、兼容性或安装包大小的实测结论。

## 2. 候选项目与成熟度

Star 来自当日 GitHub REST API，精确快照见[调研证据 JSON](2026-09-27-redlotus-desktop-pet-sources.json)。主候选以约 1,000 star 为筛选参考，另外保留同栈项目和小型对照案例。所有下表项目查询时均未归档。

“默认分支提交”与 pushed_at 分开看；更新 README、依赖或素材也会产生新提交，不能单凭日期宣称核心代码仍在积极维护。表中日期为 UTC。

| 项目 | Star | 技术与运行位置 | 默认分支提交 / 已核实稳定发行 | 对 RedLotus 的价值与限制 |
| --- | ---: | --- | --- | --- |
| [ayangweb/BongoCat](https://github.com/ayangweb/BongoCat) | 23,648 | Vue、Rust、Tauri；独立桌面窗口 | 2026-04-28 / v1.1.0，2026-04-20 | 发行和桌面交互参考强；代码 MIT。Live2D、前端及 Rust 构建链超出当前小型 Python 桌宠需要。README 的 Linux 支持明确为 X11。 |
| [LorisYounger/VPet](https://github.com/LorisYounger/VPet) | 6,844 | C#、WPF；Windows 桌宠 | 2026-09-26 / GitHub latest 未返回稳定版，README 提供 Steam、NuGet 分发 | 成熟的摸头、提起和动画机制，Core 可嵌入 WPF。代码 Apache-2.0；自带动画另有授权。跨语言嵌入不是当前最短路径。 |
| [tonybaloney/vscode-pets](https://github.com/tonybaloney/vscode-pets) | 4,171 | TypeScript；VS Code Webview | 2026-08-07 / 1.36.0，2026-07-24 | 最贴近像素角色与类继承需求。代码 MIT；宿主是 VS Code，不能直接作为 Python 桌面窗口使用，素材另行核实。 |
| [adryd325/oneko.js](https://github.com/adryd325/oneko.js) | 1,321 | JavaScript；网页 DOM | 2025-10-29 / GitHub latest 未返回稳定版 | 小型像素猫、追鼠标与待机动画。MIT 代码适合局部参考或移植；没有原生桌面窗口或成熟的 Python 包接口。 |
| [Adrianotiger/desktopPet](https://github.com/Adrianotiger/desktopPet) | 1,152 | C#；eSheep 桌宠 | 2026-09-11 / v1.4.0，2026-09-02 | 2015 年创建，精灵图、XML 动画及多屏行为有参考价值。API 未识别许可证，检查的目录树未发现标准许可证文件，暂不安排代码或素材复制。 |
| [ChaozhongLiu/DyberPet](https://github.com/ChaozhongLiu/DyberPet) | 983 | Python、PySide6 | 2026-09-25 / v0.10.3，2026-08-12 | 接近千星、同栈参考。GPL-3.0；README 仍标注公开源码至 v0.6.7、部分 LLM 功能未完全公开，不能把最新发行功能全部当作可复用源码。 |

补充筛选：

- [isHarryh/Ark-Pets](https://github.com/isHarryh/Ark-Pets)：1,102 star，Java、GPL-3.0，角色和运行栈偏离本次轻量像素需求，不进入优先阅读清单。
- [1ilit/Desktop-Cat](https://github.com/1ilit/Desktop-Cat)：101 star，MIT，Tkinter 小例子，最后默认分支提交为 2022-11-29。可作最小实现对照；README 把鼠标互动列为待改进内容，不能当作成熟完整方案。

本轮没有运行候选项目、审计其全部依赖或验证公开发行包。以上“成熟”主要依据项目年龄、公开源码、发行记录与功能完整度，star 只反映关注度。

## 3. 具体参考与复用边界

| 优先级 | 已阅读的源码或文档 | 值得带入的设计 | 复用判断 |
| --- | --- | --- | --- |
| 1 | VS Code Pets：[basepettype.ts](https://github.com/tonybaloney/vscode-pets/blob/2c91214beb922288cca1938cddb607abc5f806b7/src/panel/basepettype.ts)、[states.ts](https://github.com/tonybaloney/vscode-pets/blob/2c91214beb922288cca1938cddb607abc5f806b7/src/panel/states.ts)、[cat.ts](https://github.com/tonybaloney/vscode-pets/blob/2c91214beb922288cca1938cddb607abc5f806b7/src/panel/pets/cat.ts) | 宠物基类、角色子类、动作结束与恢复；把角色差异放在子类和动画数据 | MIT 代码可按许可条件选择性改写，保留来源与版权说明；DOM/Webview 部分不搬入 Qt。 |
| 2 | oneko.js：[oneko.js](https://github.com/adryd325/oneko.js/blob/5281d057c4ea9bd4f6f997ee96ba30491aed16c0/oneko.js) | 精灵帧索引、节制的动画更新、待机与睡眠变化 | MIT 代码可局部参考或移植；默认跟随鼠标的移动不是本次必需能力。 |
| 3 | BongoCat：[tauri.conf.json](https://github.com/ayangweb/BongoCat/blob/44f44bcf2b17b8e16463ad479a477a949d01cc9a/src-tauri/tauri.conf.json)、[Windows 窗口实现](https://github.com/ayangweb/BongoCat/blob/44f44bcf2b17b8e16463ad479a477a949d01cc9a/src-tauri/src/plugins/window/src/commands/windows.rs) | 透明、无边框、置顶、任务栏行为和平台适配位置 | 学习职责和平台差异。Windows 实现有周期性置顶逻辑，不将其直接照搬为 RedLotus 的常驻轮询策略。 |
| 4 | DyberPet：[PetWidget 与鼠标处理](https://github.com/ChaozhongLiu/DyberPet/blob/012dbbc72046de6042e46643cb0fa83098d58f6d/DyberPet/DyberPet.py) | QWidget 子类、进入/离开、按下/移动/松开事件，透明窗口属性 | 阅读行为和 Qt 原生 API；当前不计划复制 GPL 实现进 MIT 代码。若以后决定整体复用，需要重新确定衍生代码的发行许可。 |
| 5 | VPet：[README 与动画条款](https://github.com/LorisYounger/VPet/blob/be500dd6051273ea26367fe6d252a0d3585612e3/README.md)；eSheep：[AnimationXML.cs](https://github.com/Adrianotiger/desktopPet/blob/c8e43f0dd30f9051b0f90604ee7d2515039679f9/src/LocalData/AnimationXML.cs) | 提起等交互语言、素材和行为分离 | 用于功能参考。首版不采用 WPF Core、完整 XML 编辑器、游戏养成与角色素材。 |

素材判断须单列：

- VS Code Pets 的 [media/README.md](https://github.com/tonybaloney/vscode-pets/blob/2c91214beb922288cca1938cddb607abc5f806b7/media/README.md) 明确说明猫咪作者要求不要在 GitHub 自由分发，定制需取得原素材；[狗素材许可证](https://github.com/tonybaloney/vscode-pets/blob/2c91214beb922288cca1938cddb607abc5f806b7/media/dog/license.txt) 又是 CC BY-ND 4.0。不能统一按代码 MIT 处理。
- VPet 的自带动画与图片独立授权，商用、告知来源及分发有额外条件；首版采用原创素材更直接。
- oneko 等历史角色与第三方模型也应追溯素材来源。代码许可不能替代素材核实。
- 最终使用的外部代码记录仓库、固定提交、文件、修改范围和许可证；本轮没有复制第三方实现或素材。

## 4. 三条实现路线

| 路线 | 优点 | 成本与限制 | 建议 |
| --- | --- | --- | --- |
| **Python + PySide6 独立进程** | 保持现有语言，QWidget 原生继承与鼠标事件，透明绘制和屏幕/DPI API 较完整 | 新增可选 Qt 依赖、发行包体积与 Qt 许可材料；仍需实机验证平台行为 | **推荐正式首版**。只使用 QtCore、QtGui、QtWidgets，不加 Fluent Widgets、Live2D 或游戏引擎。 |
| Python + Tkinter 独立进程 | Windows 小演示代码少，部分 Python 发行版已有 Tk | 不能假定所有环境包含 Tk；透明色、平台和 DPI 细节仍需处理，找到的同类例子成熟度较低 | 仅当首要目标是最快 Windows 演示、且接受较窄平台范围时采用。 |
| Tauri 独立伴随应用 | 可以保留 TypeScript 动作代码，更接近 BongoCat 的窗口栈 | 额外 Rust/前端工具链、独立打包、跨语言消息；对单角色小窗口成本较高 | 等 RedLotus 真正需要独立桌面前端时再评估。 |

终端内部的字符宠物可以更简单，但精确像素画布与鼠标行为依赖终端能力。像素图还涉及 [kitty 图形协议能力探测](https://sw.kovidgoyal.net/kitty/graphics-protocol/) 等适配，因此不选为本次默认方向，也不同时维护第二套渲染器。

Qt 官方说明了[透明 QWidget 的平台条件](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QWidget.html)和[GUI 必须在主线程运行](https://doc.qt.io/qt-6/threads-qobject.html)。将 Qt 放入独立进程的主线程，可以让 RedLotus 的 Textual/asyncio 循环保持现有归属。Qt for Python 采用 [LGPLv3/GPLv3 或商业许可](https://doc.qt.io/qtforpython-6/)，发行时按实际使用组件及打包方式处理通知和库分发要求。

## 5. 首版功能契约

### 显示

- 同时显示一只像素宠物，提供深色外套与米色针织衫两套模板角色资源；使用一个透明、无边框、置顶的小窗口，建议先验收 Windows。
- 宠物显示区域**不超过 100×100 物理屏幕像素**。当前人物模板统一转换为 100×100 透明 PNG 播放帧，保留动作留白与统一画布；像素预览采用最近邻放大，不使用平滑插值。早期 32×32 原画建议不作为这两套人物资产的限制。
- Qt 窗口坐标是设备无关像素，不能简单调用 100×100 后宣称满足物理尺寸。按窗口所在屏幕 DPR 换算；小数缩放时允许窗口向下取整，并实测保持完整画面与尺寸上限。参见 [Qt High DPI](https://doc.qt.io/qt-6/highdpi.html)。
- 初始放在当前屏幕工作区右下角；拖动后在本次运行内保留位置。跨屏重算 DPI，屏幕拔除后放回仍存在的工作区。
- 展示与点击动画保持终端输入焦点；菜单操作结束后可继续终端输入。透明窗口与不抢焦点需要实际验收。
- 首版按小窗口矩形接收鼠标事件。精确到每个透明像素的点击穿透不纳入首版要求。

### 动作与鼠标

| 事件 | 动作 | 结束或中断规则 |
| --- | --- | --- |
| 启动、通常空闲 | Idle：眨眼 | 无互动 30 秒进入 Sleep；各帧时长写在随包动画资源描述中。 |
| 鼠标进入宠物窗口 | Look：转头/抬头 | 播完保持末帧；离开回 Idle。睡眠时进入同样可以唤醒。 |
| 左键单击 | Happy：开心/轻跳 | 完整播放一次；结束后按鼠标位置回 Look 或 Idle。 |
| 左键按住并拖动 | Drag：被提起 | 越过系统拖拽阈值才开始移动；松开回 Look 或 Idle，不再补发单击。 |
| 无互动的睡眠阶段 | Sleep：打盹 | 鼠标进入、点击或拖动立即打断，随后按对应动作处理。 |
| 右键 | 仅提供“退出桌宠”菜单 | 关闭宠物，RedLotus 对话继续运行；取消菜单恢复原动作。 |

动作优先级：退出 > 拖动 > 单击反馈 > 悬停 > 待机/睡眠。连点不累计动画队列，当前 Happy 播放期间合并重复点击；Drag 可以打断 Happy。首次按下记录点击位置，使用系统拖拽距离区分单击与拖动。

首版只做外观陪伴。聊天、语音、任务执行、养成数值、商店、多宠物、模型插件、桌面爬墙和全屏追鼠标均不在初版范围。

### 资产与资源

- 一套 PNG 精灵图和简单动画描述：动作名、帧矩形、每帧时间、循环/结束行为。
- 素材在启动时解码并缓存，换帧才请求重绘；不逐帧读磁盘，不要求网络。
- 角色资源来自随包目录；不加载任意脚本，不增设用户配置来源。
- 帧时长和动作停留时间作为公开美术数据管理；首版不添加一套参数配置 UI。
- CPU、内存、启动时间和包体积必须在原型上测量，本轮不写成已达到的性能数字。

## 6. 类设计与 RedLotus 耦合

### 有职责的继承

| 类 | 职责 | 继承或拥有关系 |
| --- | --- | --- |
| PetModel | 公开当前动作、帧、尺寸、时间推进与互动契约 | 纯 Python 抽象基类，不依赖 Qt、语音或模型服务。 |
| SpritePet | 首版两套角色的数据加载、动作优先级和结束恢复 | 继承 PetModel；资源选择来自角色 ID，图集解码与帧切分在工作线程完成。 |
| SpriteAnimation | 精灵帧、时间推进、循环与结束状态 | 由宠物/窗口使用；共享数据结构，不为每个动作建立一个类。 |
| PetService / ProcessPetService | 异步启停、选角、状态、就绪回执与退出回收 | 抽象服务与进程实现，由 AgentCliController 持有；父进程不导入 Qt。 |
| PetFactory | 创建服务、异步加载指定角色模型 | 简单静态工厂，只选择当前实现，不维护插件注册表。 |
| PetWindow | 透明绘制、DPI、局部鼠标事件、拖动和窗口生命周期 | 继承 QWidget，拥有 PetModel 与帧图像缓存；事件由实例方法处理。 |

符合“少用零散函数，多用类继承”的方向：业务状态归类、复用框架原生继承；进程服务与宠物是拥有关系，不让宠物继承 AgentSystem。只保留必要的薄启动入口，不构造通用插件工厂、多重继承、每动作一个空子类或全局事件总线。

~~~mermaid
flowchart LR
    CLI["CLI / InteractiveRepl"] --> Controller["AgentCliController"]
    TUI["RedLotusTui"] --> Controller
    Controller --> Commands["SlashCommands：/pets"]
    Commands --> Process["PetService / ProcessPetService"]
    Factory["PetFactory"] --> Process
    Process -->|"启动 / 关闭；继承管道"| Window["子进程：PetWindow / Qt 主循环"]
    Window --> Pet["SpritePet 继承 PetModel"]
    Factory --> Pet
    Pet --> Sprite["SpriteAnimation 与原创 PNG"]
    Future["后续：执行状态适配"] -.-> Process
~~~

### 首版接入

两种终端共用现有命令分发：

| 命令 | 行为 |
| --- | --- |
| /pets | 切换开启或关闭；任务执行期间也可使用。 |
| /pets on [charcoal\|ivory] | 按需启动一个子进程或切换角色；省略角色沿用当前选择，首次为 charcoal。收到窗口就绪回执后报告成功，重复调用不重复创建。 |
| /pets off | 关闭当前控制器拥有的宠物并回收资源；重复调用安全。 |
| /pets status | 返回本实例的未启动、启动中、运行中或失败状态；保留具体失败原因。 |

默认不自动启动，不添加自启服务或跨应用全局单例。一个 RedLotus 进程管理一只宠物；切会话或切项目不重建。多开 RedLotus 时各自拥有子进程，不在首版建立跨进程抢占规则。

现有接入位置已经核实：

- [console.py](../../../src/redlotus/ui/console.py)：AgentCliController 是 CLI/TUI 共用控制器，通过 PetFactory 创建并持有 PetService；/pets 加入 BUSY_SAFE_COMMANDS，运行任务时也可启停宠物。
- [cli_commands.py](../../../src/redlotus/ui/cli_commands.py)：SlashCommands 增加实例方法和分发表项，不另做命令解析框架。
- [tui.py](../../../src/redlotus/ui/tui.py)：复用同一命令入口，不建立第二套宠物状态或帧刷新任务。
- [api/base.py](../../../src/redlotus/api/base.py)：现有 run_cli/main 与退出流程可接入清理，但宠物本身不依赖 AgentSystem、语音服务或配置向导。
- [main.py](../../../main.py)：冻结程序启动子进程时需要显式的桌宠入口分流，必须在普通 Agent 启动与配置引导之前完成。
- [pyproject.toml](../../../pyproject.toml)：桌宠依赖为 `pets = ["PySide6>=6.8,<7"]`；`all` 与 `build` 同时包含它，基础文字安装和未开启桌宠的运行路径不加载 Qt。

管道只承担进程控制和就绪/错误回执，首版无需 HTTP、WebSocket 或监听端口。桌宠 stdout 专用于结构化回执，诊断走 stderr 并由父进程处理，不能污染 Textual 屏幕。就绪意味着资源加载成功且窗口已创建，不能仅依据进程仍存活判断。

服务公共入口保持异步，启动最多等待 15 秒就绪；正常退出、/pets off 和终端异常关闭都须回收。发送关闭后最多等待 2 秒，必要的终止只针对已持有的子进程句柄；父通道 EOF 使子进程自行退出。阻塞读管道不得占用 Qt 主循环，跨线程消息通过 Qt 信号交给界面线程。取消、重复命令与切换角色保留单子进程所有权，宠物崩溃只改变桌宠状态，不停止 Agent，不自动陷入重启循环。

`stop()` 是可再次开启的普通关闭；主程序退出调用异步 `close()`，永久结束该控制器的所有权并拒绝迟到的开启请求，防止 TUI 退出期间重新创建子进程。

源码/pip 使用当前解释器执行 `python -m redlotus.pets.desktop`，可附加 `charcoal` 或 `ivory`；PyInstaller 用自身可执行文件的 `--pets-child` 入口，不能对 Agent.exe 使用 Python 的 -m 语义。该分流先于普通 Agent、配置向导和语音初始化，独立桌宠模式不要求模型 Key。

### 后续状态联动

后续版本才添加单向状态适配：由执行生命周期产生 idle、running、waiting_input、completed、failed 等摘要，再映射为宠物动作。适配放在 UI/控制器边界；不轮询日志，不解析模型正文，不把提示词、工具参数、历史记录或凭据送给宠物。

手动鼠标动作优先；其间只保留最新任务状态，鼠标动作结束后再展示。失败不会伪装成成功庆祝，多个 Worker 不逐个抢动作；以当前前台会话的汇总状态为准。切换会话重置摘要，过期状态不能覆盖新会话。

这是后续耦合方向，不在 v0 写事件总线、任务指令接口或任何自动 Agent 操作。首版保留的真实边界是 PetService 与纯宠物模型，无需提前实现未来消息。

## 7. 代码落位与现有结构约束

桌宠使用第 10 个应用模块 `pets`，隔离 GUI 依赖和独立生命周期；`ui` 继续保留原有五文件职责。[开发约定](../../development.md#代码组织与精简)与[结构检查](../../../scripts/check_structure.py)将模块上限从 9 调整为 10，每模块递归最多 5 个 Python 文件、每文件最多 500 有效行的限制保持不变。

`pets` 为命名空间包，无 `__init__.py`、无 `__main__.py`。唯一的模块执行入口直接放在 `desktop.py`；代码与美术资源分别落位，不将业务逻辑放入 JSON 或静态目录规避统计。

固定职责布局：

~~~text
src/redlotus/pets/
    model.py       PetModel、SpritePet、SpriteAnimation
    desktop.py     PetWindow、Qt 生命周期与控制通道接收
    service.py     PetService、ProcessPetService；异步控制单个子进程
    factory.py     PetFactory；创建服务与异步加载角色模型
src/redlotus/static/pets/
    pets.json
    ASSET-NOTICE.md
    charcoal/pet.json、sprites.png
    ivory/pet.json、sprites.png
~~~

运行时资源经 `runtime.resources.resource_root()` 定位，源码、wheel 和冻结程序共用同一目录结构。直接维护上述六个运行时文件，JSON 不包含 `frames[*].file`、`provenance.reference`、`provenance.generation`，保留指向运行时 `ASSET-NOTICE.md` 的说明路径。

setuptools 和 `MANIFEST.in` 只收集上述运行时文件。PyInstaller onedir 与 onefile 共用 `build.spec`，显式包含四个桌宠模块及 QtCore、QtGui、QtWidgets，依赖官方 hooks 收集所需 DLL 和平台插件，并保留 PySide6、PySide6_Essentials、shiboken6 的发行元数据及许可文件。语音运行库和权重排除规则保持原有边界。

## 8. 开发顺序与验收

实施与验证按以下顺序推进；自动回归和实机验收分别记录：

1. **宠物模型与资源**：接入两套既有角色与五种动作，验证帧序列、动作中断、连点合并与资源缓存。
2. **Qt 与生命周期**：验证透明窗口、100 像素尺寸、鼠标事件、焦点、DPI、管道和子进程关闭。
3. **RedLotus 接入**：/pets 命令、可选依赖、控制器所有权、源码/pip/冻结入口与退出回收。
4. **后续版本**：验收首版之后再设计任务状态联动；不把它作为初版交付条件。

首版验收应覆盖：

| 场景 | 可观察结果 |
| --- | --- |
| 显示与缩放 | 100%、125%、150%、200% 缩放下量测窗口和宠物；不超过 100×100 物理像素，无裁切、无平滑模糊。 |
| 鼠标事件 | 悬停、离开、单击、连点、拖动、移出后松开、睡眠唤醒均符合动作表；拖动结束不误触单击。 |
| 终端并存 | PyCharm Terminal 与 Windows Terminal 中测试；任务执行、输入草稿与焦点保持可用，动画不刷入终端。 |
| 生命周期 | 重复 on/off、切会话、切项目、父进程退出、异常关闭、宠物崩溃均不泄漏子进程或影响 Agent。 |
| 安装与入口 | 纯文字安装可正常运行；缺少 Qt 时 /pets on 给出安装指引；独立桌宠模式无需模型配置。 |
| 打包与资源 | 源码、安装 wheel、onedir、onefile 分别核对资源路径、Qt 插件加载、退出清理和第三方许可材料。 |
| 屏幕变化 | 拖至不同 DPI 屏幕、负坐标副屏与拔除显示器后，宠物仍可见、大小受控。 |
| 平台声明 | 首版以 Windows 实测为准；macOS、Linux X11、Wayland 分别验证，未测不得宣称已支持。 |

动作转换适合小型纯模型测试；资源与分发测试核对运行时文件和清单，真实 Qt 冒烟测试核对窗口就绪与退出。透明、焦点、物理 DPI 和跨屏必须实机核实，不能用模拟测试或单次无头启动替代。
