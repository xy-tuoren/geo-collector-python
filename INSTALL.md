# Geo Collector 安装与首次使用

## 最简单上手：直接发给任意本地 Agent 一句话（免手敲命令、不限特定 Agent）

只要你的本地 Agent **能执行终端命令、读写文件和看截图**（例如 Claude Code、Codex、Cursor、Antigravity、Windsurf、OpenCode、Trae 或任意其他同类 Agent），无需拉源码、无需额外模型或 API Key，直接把下面这段话发给它即可，由当前 Agent 自行按自身规范完成安装、配置技能与手机自检：

> 帮我安装并配置 PyPI 包 `xiaohe-geo-collector`（Python 3.11，国内请用清华镜像 `https://pypi.tuna.tsinghua.edu.cn/simple`；如缺少 `uv` 或 `adb` 请顺手帮我装好）。装好后按你自己支持的 Skill 或规则机制给我配好 `geo-collector`：如果你使用 skills 目录，直接运行 `geo-collect setup --dir <你自己的 skills 目录>`；否则运行 `geo-collect agent-guide` 读取完整说明并按你的规范保存（或创建一条规则在采集时调用 `geo-collect agent-guide`）。最后运行 `geo-collect doctor` 帮我检查手机连接。

（如果更喜欢手动放文件，也可直接把 `.agents/skills/geo-collector` 文件夹放到对应 Agent 的 Skills 目录。）

## 终端手动安装 CLI 和配套 Skill

CLI、配套 `SKILL.md` 与按需排障参考 `references/install.md` 打包在同一个 PyPI wheel 中（包名 `xiaohe-geo-collector`）。

### 1. 首次安装

`uv` 会自动准备 Python 3.11 和依赖；安装好 CLI 后，可用 `geo-collect setup --dir <任意 Agent 的 skills 目录>` 把随包 Skill 写入任意目录，或直接让 Agent 运行 `geo-collect agent-guide` 按自身规范配置：

```sh
# 国内网络推荐加清华镜像（首次下载 onnxruntime/opencv 更快）
uv tool install --python 3.11 --default-index https://pypi.tuna.tsinghua.edu.cn/simple xiaohe-geo-collector

# 国际网络
uv tool install --python 3.11 xiaohe-geo-collector

# 方式 A：安装 Skill 到任意 Agent 的 skills 目录（不限特定 Agent）
geo-collect setup --dir <你的 Agent skills 目录>

# 方式 B：使用内置的常用目录快捷方式（仅为路径快捷方式，并非支持范围上限）
geo-collect setup --agent codex
```

内置 `--agent` 快捷方式对应的默认目录：
- `codex` → `~/.agents/skills/geo-collector/`
- `claude-code` → `~/.claude/skills/geo-collector/`（尊重 `CLAUDE_CONFIG_DIR`）
- `cursor` → `~/.cursor/skills/geo-collector/`
- `antigravity`（或别名 `gemini`）→ `~/.gemini/skills/geo-collector/`

不支持 `SKILL.md` 目录的 Agent 无需执行 `setup`，直接运行 `geo-collect agent-guide` 读取同一份操作规范即可。

Windows PowerShell 5 不支持 `&&`，请分行执行。如果 uv 提示工具目录不在 PATH，先执行 `uv tool update-shell` 并重新打开终端或本地 Agent，再运行 `geo-collect`。

> 处于离线/内网环境时，也可把包名替换成 GitHub Release 提供的本地 wheel 路径（如 `./xiaohe_geo_collector-0.1.6-py3-none-any.whl`）。

### 2. 一键更新（升级 CLI 并同步 Skill）

维护者发布新版本后，已安装用户只需一条命令即可升级 CLI，并自动刷新常用目录中已托管的 Skill（不影响各自工作目录里的 `.geo-collector/rules.json`、`lessons.json` 和 `captures/`）；使用 `--dir` 自定义目录的用户重跑一次 `geo-collect setup --dir <原目录>` 即可同步，也可以直接在对话里让 Agent 完成升级：

```sh
# 国内网络使用清华或阿里镜像加速
geo-collect upgrade --mirror tuna

# 国际网络
geo-collect upgrade
```

也可以手动分步执行：

```sh
uv tool upgrade xiaohe-geo-collector && geo-collect setup --installed
```

装好后，在计划保存数据的目录重新打开 Agent 会话，连接并授权安卓手机，然后直接发送：

> 使用 geo-collector 采集这些问题，入口为小荷 App、抖音搜索、头条搜索和抖音小程序直问：[问题列表]。先检查手机连接，遇到异常自行检查、修复并恢复原批次，验收后给我交付图片和逐题结果。

用户无需手动创建 Skill 或填写修复 JSON。需要登录、解锁或授权 USB 调试时，Agent 会在当前对话中请求协助。Skill 提供安装自检、采集、自愈和验收流程，必须由正在运行的本地 Agent 读取和执行。

`setup --dir <路径>` 可安装到任意目录；使用 `--agent` 快捷方式时默认安装到当前用户目录，`--scope project` 仅安装到当前工作目录；`--installed` 一键刷新常用目录中由 `geo-collector` 托管的 Skill；`--dry-run` 可查看目标而不写文件。用户自行改过的同名 Skill 或附属文件会被保留并报告冲突，符号链接部署应通过原 Skill 管理工具更新。

## 维护者：自动发布到 PyPI 与 GitHub Release

仓库已配置 `.github/workflows/ci.yml` 与 `.github/workflows/release.yml`（采用 PyPI OIDC Trusted Publishing，无需长期 API Token）：

1. **一次性配置 PyPI Trusted Publisher**：登录 [PyPI](https://pypi.org/manage/account/publishing/)，新增 Pending Publisher：
   - PyPI Project Name：`xiaohe-geo-collector`（以 `pyproject.toml` 中的 `project.name` 为准）
   - Owner：`xy-tuoren`
   - Repository：`geo-collector-python`
   - Workflow name：`release.yml`
   - Environment name：`pypi`（如需先试发 TestPyPI，可在 [TestPyPI](https://test.pypi.org/manage/account/publishing/) 同理绑定 `testpypi` 环境）
2. **在 GitHub 仓库创建 Environment**：在仓库 `Settings → Environments` 中新建 `pypi`（及可选 `testpypi`）。
3. **每次发版一条命令**：

```sh
# 自动更新版本与 uv.lock、跑全量检查并构建 wheel/ZIP，再按提示推送 tag 触发自动发布
uv run python scripts/release.py 0.1.3 --push
```

推送 `v0.1.3` 后，GitHub Actions 会自动跑全量测试、验证 wheel 独立安装、发布到 PyPI 并创建带 `.whl` / `.tar.gz` / `-setup.zip` 与 `SHA256SUMS.txt` 的 GitHub Release。

## ZIP 安装方式

选择 ZIP 时，把包解压到自己有写权限的目录。首次安装需要联网下载 Python 和锁定的依赖。下文的 `geo-collect` 命令，在解压目录中用 `uv run --locked --no-dev geo-collect` 调用。

### Windows x64

1. 双击 `install-windows.cmd`。已有 `uv` 和 `adb` 时直接复用，缺少时安装器使用 Windows 的 WinGet 安装它们，然后在解压目录创建 `.venv`。看到 `Geo Collector installed` 后，打开一个新终端或重新打开本地 Agent。
2. 如果电脑没有 WinGet，先安装 Microsoft 的 [App Installer](https://learn.microsoft.com/windows/package-manager/winget/)。也可以按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/)和 [Android Platform-Tools 下载页](https://developer.android.com/tools/releases/platform-tools)手动安装，确认新终端能运行 `uv --version` 和 `adb version` 后再双击安装器。
3. 如果安装器在导入 `onnxruntime` 时报告缺少运行库，用 WinGet 安装 `Microsoft.VCRedist.2015+.x64` 或使用 [Microsoft Visual C++ 运行库安装程序](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)，再重试。

Windows 的异常接管由本地对话 Agent 执行。独立的 `--agent codex` 和 `supervise` 模式目前支持 macOS 和 Linux。

### Linux 和 macOS

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)和 [ADB](https://developer.android.com/tools/releases/platform-tools)。在 Ubuntu 可使用系统的 `adb` 与 `android-sdk-platform-tools-common`（USB 设备规则），在 macOS 可用 Homebrew 安装 `uv` 与 `android-platform-tools`。如果 Linux 导入 OpenCV 时报告缺少 `libGL.so.1`，在 Ubuntu 安装 `libgl1` 再重试。

在解压目录执行以下命令。

```sh
uv sync --locked --no-dev
uv run --locked --no-dev geo-collect entries
```

## 连接手机并完成首测

1. 在安卓手机上安装要采集的 App。分别是小荷 AI 医生、抖音和今日头条，抖音小程序入口使用同一个抖音 App。
2. 开启开发者选项中的 USB 调试，用支持数据传输的线连接手机。解锁手机，在手机上允许这台电脑调试，并保持竖屏。
3. 在计划保存采集数据的工作目录运行 `geo-collect doctor`，检查连接、截图、页面结构和 OCR。ZIP 安装者从解压目录调用。

CLI 优先使用指定的或系统已有的 ADB。Windows x64 和 Linux x64 的 `adbutils` 依赖自带 ADB，CLI 可以在系统 ADB 缺失时使用。其他平台如果自检提示找不到 ADB，请安装 [Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools)。

ADB 显示 `unauthorized` 时，先在手机上确认授权。Windows 上看不到设备时，可参考 [安卓 USB 驱动说明](https://developer.android.com/studio/run/oem-usb)安装手机厂商的驱动。Ubuntu 提示 `no permissions` 时，检查 `android-sdk-platform-tools-common` 和当前用户的 `plugdev` 组权限。全局安装者也可使用上述 Visual C++ 或 OpenCV 缺少运行库的排查方式。

## 发给本地 Agent 的采集任务

把用于存放采集数据的目录作为本地 Agent 工作区，确保 Agent 可以执行终端命令并查看本机文件和截图。全局安装的用户可以直接把这段话发给 Agent。

完成 `setup` 的用户使用前面的采集任务即可。其他支持终端和图片的 Agent，或尚未发现 Skill 的会话，可以用下面的兜底方式加载同一份说明：

> 请先运行 `geo-collect agent-guide`，然后按说明采集我给的问题。入口是小荷 App、抖音搜索、头条搜索和抖音小程序直问。遇到异常请检查现场、修复并继续原批次，最终验收后给我交付图片目录和逐题结果。

在终端使用时，把题目写在 UTF-8 的 `captures/questions.txt` 中，每行一题，再运行以下命令。

```sh
geo-collect run --entry xiaohe douyin toutiao douyin-direct --questions-file captures/questions.txt
```

交付图片保存在 `captures/` 下的批次目录中。终端运行遇到新页面时会保存断点，退出码 `20` 代表需要 Agent 接管。用户可以把批次目录告诉同一个工作区的本地 Agent，由它查看现场并继续原批次。

每位用户的题库、截图、断点及学到的规则留在自己的工作目录。全局安装更新时，执行 `geo-collect upgrade`（国内加 `--mirror tuna`），如通过 `--dir` 安装到自定义目录再重跑 `geo-collect setup --dir <原目录>` 即可，沿用原工作目录。ZIP 安装跨目录升级时可复制自己的 `.geo-collector/` 来复用已验证规则。已有暂停批次从原目录恢复。
