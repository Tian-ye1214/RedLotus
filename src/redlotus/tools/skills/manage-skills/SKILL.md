---
name: manage-skills
description: "搜索 / 安装 / 更新 / 删除技能到本项目 src/skills/<slug>/。发现用 npx skills find；安装从技能所在的 GitHub 公开仓库免鉴权拉取（tree API 定位 + raw.githubusercontent 下载，git sparse-checkout 兜底）。辅源 ClawHub 用 npx clawhub --dir skills。所有读写仅限 src/skills/ 内、绝不动 *.py；每次增删改后必须调用 refresh_skills 热加载。用于：'找/装一个 X 技能'、'更新某技能'、'删掉某技能'、'整理技能'。"
homepage: https://www.skills.sh/
---

# 管理技能（manage-skills）

把热门注册表的技能下载、更新、删除到本项目的 **`src/skills/<slug>/`**（每个技能一个文件夹，根目录含 `SKILL.md`）。本项目的 `SkillsManager` 只加载 `src/skills/<slug>/SKILL.md`，所以一切都要落在这里。

> **重要前提**：skills.sh 的 HTTP API（`/api/v1/skills/...` 搜索与详情）**需要 Vercel OIDC 鉴权**，普通环境用不了。因此发现走 `npx skills find` CLI，安装直接从技能的 **GitHub 公开仓库**拉取（免鉴权）。

## 铁律（每次操作都遵守）

1. **沙箱**：只读写 `src/skills/<slug>/` 内的文件。**绝不**修改 / 删除 `SkillsManager.py`、`SkillsTools.py`、`__init__.py`。
2. **保护工具技能**：不要删除 `manage-skills`（自身）、`skill-creator-operator`、`skill-template`、`skill-vetter-1.0.0`。
3. **收尾必刷新**：任何 增 / 改 / 删 之后，调用 `refresh_skills` 工具，使技能清单即时更新。
4. **命令在仓库根目录执行**（cwd = 本项目根，即包含 `src/` 的目录）。

## 标识约定

- 安装时**文件夹名 = 该技能 `SKILL.md` frontmatter 的 `name`**（清洗为 kebab-case：小写、空格转 `-`、去掉非 `[a-z0-9-]`）。避免「文件夹名」与「注册名」分叉。
- 安装后系统以 frontmatter `name` 注册该技能；之后用该 `name` 调 `get_skill_instructions(name)`。
- 删除 / 更新按**文件夹名**定位 `src/skills/<slug>/`。

---

## C —— 安装

### skills.sh（主）—— 从 GitHub 公开仓库拉取

1. **发现**：
   ```bash
   npx --yes skills find "<关键词>"
   ```
   输出每条形如（可直接解析）：
   ```
   <owner>/<repo>@<skill>   <N> installs
   └ https://skills.sh/<owner>/<repo>/<skill>
   ```
   选定一条，得到 `owner`、`repo`、`skill`。`owner/repo` 即 GitHub 公开仓库 `github.com/<owner>/<repo>`（绝大多数源是 GitHub）。

2. **定位仓库内子路径并列出文件**（tree API，免鉴权）：
   ```bash
   curl -s "https://api.github.com/repos/<owner>/<repo>/git/trees/HEAD?recursive=1"
   ```
   在返回 `tree[]` 中找到路径以 `/<skill>/SKILL.md` 结尾（或顶层 `<skill>/SKILL.md`）的那一项，确定该技能在仓库里的**子目录前缀** `<subdir>`（常见就是 `<skill>`，少数仓库是 `skills/<skill>`）。再筛出所有 `type=="blob"` 且 `path` 以 `<subdir>/` 开头的文件，作为待下载清单。

3. **逐个下载到 `src/skills/<slug>/`**（raw，免鉴权、支持二进制）：
   对清单里每个 `<subdir>/<rel...>`：
   ```bash
   curl -s "https://raw.githubusercontent.com/<owner>/<repo>/HEAD/<subdir>/<rel...>"
   ```
   写入 `src/skills/<slug>/<rel...>`（去掉 `<subdir>/` 前缀，自动建子目录）。务必确保写出 `src/skills/<slug>/SKILL.md`。

4. **兜底（文件极多 / 含大二进制时用 git）**——注意 Windows 的 dubious ownership：
   ```bash
   git clone --depth=1 --filter=blob:none --sparse https://github.com/<owner>/<repo> .tmp_skill
   git -C .tmp_skill -c safe.directory='*' sparse-checkout set <subdir>
   # 复制 .tmp_skill/<subdir>/ 内容到 src/skills/<slug>/，然后删除整个 .tmp_skill（含 .git）
   ```

5. **非 GitHub 源兜底**（`owner` 不是 GitHub 用户，如 `skills.volces.com`）：用官方 CLI 装到标准目录再搬运：
   ```bash
   npx --yes skills add "<owner>/<repo>@<skill>" -a claude --copy -y
   # 从 ./.claude/skills/<skill>/ 把整个文件夹移动到 src/skills/<slug>/，删掉 .claude 下的残留
   ```

6. **校验**：确认 `src/skills/<slug>/SKILL.md` 存在，且 frontmatter 的 `name`、`description` 均非空。任一不满足 → 删除 `src/skills/<slug>/` 回滚并报告原因。

7. **刷新**：调用 `refresh_skills`。

### ClawHub（辅）

ClawHub 原生支持 `--dir`，直接落位，无需搬运：
```bash
npx --yes clawhub --dir skills install <slug>   # 覆盖加 --force
```
完成后调用 `refresh_skills`。

---

## R —— 查

- **注册表检索**：`npx --yes skills find "<关键词>"`（skills.sh）或 `npx --yes clawhub search "<关键词>"`（ClawHub）。
- **安装前预览**：用第 2、3 步的 raw 链接先取 `<subdir>/SKILL.md` 看 frontmatter（`name` / `description`）。
- **已装技能清单 / 详情**：用本项目现有工具 `list_available_skills`、`get_skill_instructions(name)`、`load_skill_resource(name, file)`。

---

## U —— 改（更新）

本项目不维护来源清单，更新 = 按 slug 重新解析来源后重拉覆盖。

1. `npx --yes skills find "<slug>"` 匹配同名，取 `owner/repo@skill`（结果歧义时让用户指定）。
2. 删除旧目录 `src/skills/<slug>/`，按「C —— 安装」重新拉取。
3. 调用 `refresh_skills`。

ClawHub：`npx --yes clawhub --dir skills update <slug>`，完成后 `refresh_skills`。

---

## D —— 删（卸载）

1. **校验**目标：在 `src/skills/` 内、是目录、不是 `*.py`、不在「铁律 2」的保护清单里。
2. 删除整个 `src/skills/<slug>/` 目录。
3. 调用 `refresh_skills`。

ClawHub：`npx --yes clawhub --dir skills uninstall <slug>`，完成后 `refresh_skills`。

---

## 故障排查

- **skills.sh API 返回 `authentication_required`**：这是预期的——不要用 `/api/v1/...`，改用 `npx skills find` + GitHub tree/raw（见 C 节）。
- **GitHub API 限流**（未鉴权约 60 次/小时）：退避重试，或直接用 git 兜底（第 4 步）。
- **Windows git「dubious ownership」**：git 命令带 `-c safe.directory='*'`。
- **网络代理**：必要时设置 `HTTPS_PROXY` / `HTTP_PROXY`。
- **`npx` 首次运行**：会下载 CLI，稍慢属正常。
- **装完技能没出现**：确认 `src/skills/<slug>/SKILL.md` 路径正确、frontmatter 合法，再 `refresh_skills`。

## 备注：git

下载的技能在本项目被 `.gitignore` 忽略（视为个人配置），正常不会出现在 `git status` / `diff`。无需也不要为安装/删除技能去改 git 跟踪状态。
