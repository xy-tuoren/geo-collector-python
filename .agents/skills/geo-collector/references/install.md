# Geo Collector 安装、升级与设备排障参考

仅在首次安装 `geo-collect`、升级版本或 `geo-collect doctor` 失败时按需阅读本文件。日常采集无需读取。

## 1. 安装或升级 CLI（唯一官方包：`xiaohe-geo-collector`）

不要猜测私有仓库地址，也不要安装名称相近的非官方包。统一通过 `uv` 安装 PyPI 官方包 `xiaohe-geo-collector`（Python 3.11）：

```bash
# 国内网络推荐（清华镜像）
uv tool install --python 3.11 --default-index https://pypi.tuna.tsinghua.edu.cn/simple xiaohe-geo-collector

# 备用国内镜像（阿里云）
uv tool install --python 3.11 --default-index https://mirrors.aliyun.com/pypi/simple xiaohe-geo-collector

# 国际网络（官方 PyPI）
uv tool install --python 3.11 xiaohe-geo-collector
```

安装完成后，如需要把 Skill 托管到本机 Agent 以便后续随 `upgrade` 自动刷新：

```bash
geo-collect setup --agent codex        # ~/.agents/skills/geo-collector/
geo-collect setup --agent claude-code  # ~/.claude/skills/geo-collector/
geo-collect setup --agent cursor       # ~/.cursor/skills/geo-collector/
geo-collect setup --agent antigravity  # ~/.gemini/skills/geo-collector/（也支持别名 gemini）
```

升级到最新版（自动刷新所有受管 Skill，不影响 `.geo-collector/` 与 `captures/`）：

```bash
geo-collect upgrade --mirror tuna      # 国内镜像
geo-collect upgrade                    # 官方 PyPI
```

### 缺少 `uv` 或找不到 `geo-collect`

- **macOS / Linux 安装 uv**：
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # macOS 也可用 Homebrew：brew install uv
  ```
- **Windows (PowerShell) 安装 uv**：
  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  # 或：winget install astral-sh.uv
  ```
- **装完后提示 `geo-collect: command not found`**：
  - 执行 `uv tool update-shell`，重新打开终端或 Agent；
  - 也可先运行 `uv tool dir --bin` 查出实际二进制路径并直接调用。

## 2. 平台运行库与 ADB 排障

- **macOS**：缺少 `adb` 时执行 `brew install --cask android-platform-tools`。
- **Linux (Ubuntu/Debian)**：
  - OpenCV 报缺少 `libGL.so.1`：`sudo apt-get update && sudo apt-get install -y libgl1 libglib2.0-0`
  - 缺少 ADB 或提示 `no permissions`：`sudo apt-get install -y adb android-sdk-platform-tools-common`，并确认当前用户加入 `plugdev` 组后重插数据线。
- **Windows x64**：
  - `adbutils` 自带 ADB；若系统找不到设备，安装手机厂商的 Android USB 驱动或确认使用支持数据传输的 USB 线。
  - 若 `onnxruntime` 导入报错，安装 Microsoft Visual C++ 运行库：`winget install Microsoft.VCRedist.2015+.x64`。
  - PowerShell 5 不支持 `&&`，两条命令请分行执行。
  - Windows 异常接管由当前对话 Agent 完成；独立的 `--agent codex` / `supervise` 仅支持 macOS 与 Linux。

## 3. 安卓手机准备清单

在当前对话请求用户确认以下准备事项（操作手机时先保持采集暂停）：

1. 手机已安装待采集 App：小荷 AI 医生、抖音（含抖音小荷小程序入口）、今日头条。
2. 已开启「开发者选项 → USB 调试」，用支持数据传输的线连接电脑，且屏幕保持**解锁**与**竖屏**。
3. 若 `adb devices` 或 `geo-collect doctor` 报 `unauthorized`：请用户在手机弹出的「允许 USB 调试」对话框勾选始终允许并点确定。
4. 完成后运行 `geo-collect doctor` 确认能正常获取截图、页面层级和 OCR。
