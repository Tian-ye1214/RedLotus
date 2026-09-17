# 安装与打包验收记录（2026-09-17）

本记录只保留可复核的结论、退出码和摘要哈希；运行期的完整转录及缓存会在验收结束后按清理规则移除。

## 已通过：wheel 安装入口

- 最终 wheel：`redlotus-1.0.0-py3-none-any.whl`，构建 26.189 秒，静态审计确认包含 LanceDB、未包含 SQLite 扩展、`config.json` 或 `.env`。
- 新建 E 盘虚拟环境安装（含 browser extra）用了 525.246 秒，退出码 0；`pip check` 为 `No broken requirements found.`。
- 已安装的 `redlotus` 实际使用 site-packages 代码。隔离项目中完成真实模型主动记忆、`/LTM`、退出、恢复同一会话和 `search_memory`：两个进程退出码均为 0，合计 180.266 秒，无 `WARNING` 或 `ERROR`。项目仅产生一个 `model_messages.json`；LanceDB 写入隔离的评测目录。

## 未通过：`scripts/pack.ps1` 的 onedir 批次

两次失败均保留原始报告和日志摘要，未通过删除功能依赖、修改模型配置或降低验收条件规避。

| 批次 | 结果 | 证据 |
| --- | --- | --- |
| 受限网络首次运行 | 6.610 秒，exit 1 | uv 下载 `pydantic-core==2.46.3` 时被本机网络策略拒绝（`os error 10013`）。脚本正确把原生命令的非零退出码传递出来。 |
| 获准网络重跑 | 258.765 秒，exit 2，未进入 PyInstaller Analysis | E 盘本次专用 `WorkDatabase/runtime/packaging/uv-cache` 先报临时重命名 `os error 5`，随后读取 `archive-v0/XtU2bKtESdhjJfuK/redlotus-1.0.0.dist-info/METADATA` 失败，Windows 返回 `os error 1392`（文件或目录损坏且无法读取）。该路径长度约 190，当前发现的最长包装路径为 245，因此现有证据不支持将原因归为 Windows 路径上限。 |

该缓存项位于本工作树的可再生成 E 盘包装目录。构建进程退出后，PowerShell 对受损项的只读属性查询和删除均被同一 `os error 1392` 阻断；没有执行 `chkdsk`、整卷修复、迁移到 C/D 盘或更改应用代码。损坏的文件系统项的根因尚未确定。

曾准备改用已完成 wheel 验收的 E 盘虚拟环境直接调用 PyInstaller；用户随后明确要求立即合并，故未执行该替代构建。onedir / onefile 不能标记为通过，受损缓存尚未清理。
