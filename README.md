# osworld_agent：Windows 通用 GUI Agent 的移植与运行

本仓库的 `computer` profile 在 Windows 主机上执行同一个跨应用任务：普通应用使用桌面截图，按需启动的 Chrome/Edge 网页获焦点时加入精简 AX。进入地址栏、原生对话框、其他应用或结构接口失败时继续纯视觉操作。任务状态、记忆和轨迹在应用切换之间保持连续。详见 [Windows computer use 运行说明](docs/WINDOWS_COMPUTER_USE.md)。

Agent 在本机运行，决策和视觉定位通过兼容 OpenAI Chat Completions 的 HTTP API 调用。安装脚本准备 Python 环境，不部署模型或虚拟机，不要求本机 GPU。通用 GUI 模式需要已解锁、可交互且与目标应用权限一致的 Windows x64 桌面，不支持 `-Headless`。

**交给新电脑上的 Agent 自动执行时，让它完整读取 [Windows 自动跑通文档](docs/WINDOWS_AGENT_RUNBOOK.md)。** 其中包含 uv 安装、参数位置、CMD/PowerShell 命令、独立验收条件及故障处理。

| Profile | 操作对象 | 新电脑需要准备 |
| --- | --- | --- |
| `computer` | Windows 主机上的应用；浏览器为可选增强 | 交互桌面、模型 API；网页任务另需实际浏览器，登录配置可选 |
| `browser` | 独立的 Chromium 网页专项任务 | 模型 API；安装脚本准备 Playwright Chromium |
| `desktop` | VMware 客户机中的 OSWorld 任务 | 外部 `desktop_env`、任务集、完整 VM 和快照、VMware、模型 API |

脚本默认 profile 仍为 `browser`。本文主线全部显式使用 `-Profile computer`，旧浏览器表单验收不能证明 Windows 跨应用任务通过。

## 1. 仓库与版本

代码仓库：[BAYMAXer/GUI_Agent](https://github.com/BAYMAXer/GUI_Agent)，默认分支 `main`。最新增量版本 `v3.1.0` 在第三版通用 GUI 方案上增加固定工作屏、多屏坐标和前台输入保护；第一版 `v1.0.0`、第二版 `v2.0.0`、第三版 `v3.0.0` 保留。版本区别见 [版本记录](docs/VERSIONS.md)。

固定最新版本使用 `git clone --branch v3.1.0 https://github.com/BAYMAXer/GUI_Agent.git osworld_agent`；需要修改时先从标签创建自己的工作分支。不要复制旧 `.venv` 到新电脑，虚拟环境需要在目标路径重新创建。

`.gitignore` 排除 `.env`、`.venv`、本机预设、产物、外部 OSWorld 目录和 VM 文件。真实 Key 放 `.env` 或忽略的 `config/model_presets.local.yaml`；已有共享配置中的地址仍需按实际网络核对。上传前检查 `git status --short` 和 `git diff --cached`，不要把 Key 加进仓库 URL。

## 2. 新 Windows 电脑：uv 一键环境

使用 Windows x64 和 64 位 PowerShell；建议 Windows 11。准备 Git，并确保网络能访问 GitHub、Astral、Python 包源和 Playwright 下载源。Git 尚未安装时可在 PowerShell 运行下列命令，完成后重新打开终端：

```powershell
winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
```

在 **CMD** 中执行，示例路径包含中文和空格：

```bat
if not exist "%USERPROFILE%\source" mkdir "%USERPROFILE%\source"
git clone --branch v3.1.0 https://github.com/BAYMAXer/GUI_Agent.git "%USERPROFILE%\source\GUI Agent 移植"
cd /d "%USERPROFILE%\source\GUI Agent 移植"
setup.cmd -Profile computer -SkipSmoke
run.cmd -Mode doctor -Profile computer
run.cmd -Mode computer-smoke -Profile computer -Channel msedge
```

PowerShell 等效命令：

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE\source" -Force | Out-Null
git clone --branch v3.1.0 https://github.com/BAYMAXer/GUI_Agent.git "$env:USERPROFILE\source\GUI Agent 移植"
Set-Location -LiteralPath "$env:USERPROFILE\source\GUI Agent 移植"
.\setup.cmd -Profile computer -SkipSmoke
.\run.cmd -Mode doctor -Profile computer
.\run.cmd -Mode computer-smoke -Profile computer -Channel msedge
```

脚本查找兼容 uv，缺少时安装 `.uv-version` 指定版本到 `.tools/uv/`，下载 `.python-version` 中的 Python，以 `uv sync --locked` 安装 `uv.lock` 固定的依赖并创建隔离 `.venv`。computer 安装默认检查交互桌面，不注入输入；仅明确配置 Playwright Chromium 且没有自定义 exe/CDP 时下载 Chromium。使用已安装 Chrome/Edge 无需下载浏览器。无需预装 Python、pip 或 uv，无需激活 venv或修改系统执行策略；已有 `.env` 和本机预设保持原内容。当前固定 uv 0.12.23、Python 3.12.15。

`doctor -Profile computer` 检查桌面截图与前台焦点，不注入输入。`computer-smoke` 会操作自身测试记事本和浏览器：验证焦点路由与视觉回退，下载本地测试 PDF，返回记事本回写中文并保存。它使用固定策略和定位替身，`model_verified=false`，不能当成真实模型验证或模型训练样本。报告位于 `artifacts/computer-smoke/report.json`。

若已有受支持的 Python 3.12 x64，可指定 `setup.cmd -Profile computer -Python "C:\实际路径\python.exe" -SkipSmoke`。旧环境失效或继承系统包时，使用 `setup.cmd -Profile computer -RecreateVenv -SkipSmoke`，脚本在本仓库保留旧环境备份后重建。

## 3. 配置模型与按需浏览器

编辑安装生成的根目录 `.env`：

```dotenv
PLAN_MODEL=实际决策服务模型ID
PLAN_API_URL=https://实际服务/v1
PLAN_API_KEY=实际密钥
PLAN_THINKING_STYLE=none

COMPUTER_BROWSER_CHANNEL=auto
COMPUTER_MONITOR=primary
COMPUTER_BROWSER_EXECUTABLE=
COMPUTER_BROWSER_PROFILE_DIR=
COMPUTER_DOWNLOAD_DIR=
COMPUTER_CDP_ENDPOINT=
```

URL 是服务的 API base URL，不附加 `/chat/completions`。未认证端点需要明确填写 `PLAN_API_KEY=EMPTY`。`PLAN_THINKING_STYLE` 可选 `none`、`vllm`、`dashscope`，按实际服务协议设置。服务必须支持图片输入；跨普通应用的目标操作还需要视觉定位能力。

独立定位服务可填写以下字段；没有 `GROUNDING_API_URL` 时，computer 入口复用 PLAN 服务，故该服务必须同时支持决策和所选视觉定位协议：

```dotenv
GROUNDING_MODEL=实际定位服务模型ID
GROUNDING_API_URL=https://实际定位服务/v1
GROUNDING_API_KEY=实际定位密钥
GROUNDING_PROTOCOL=structured
GROUNDING_THINKING_STYLE=none
```

定位协议可选 `structured`、`pixel`、`normalized`，复用 PLAN 也读取 `GROUNDING_PROTOCOL` / `-GroundType`；`auto` 按模型注册表选择，通常为 `structured`。对任意部署名称建议显式填写实际协议。复用 PLAN 不等于安装或获得另一个定位模型；报告记录实际使用的角色和端点。不要在 CMD 命令里填写 Key。

`.env` 支持 `NAME=value`、整行注释和带引号的字面值，不支持变量展开、行尾注释或多行值。终端已有的非空环境变量优先；路径相对仓库根目录解析，可包含中文和空格。

| computer 配置 | 用途 |
| --- | --- |
| `COMPUTER_BROWSER_CHANNEL` | `auto` 尝试已安装 Chrome、Edge，再 Playwright Chromium；可固定 `chrome` / `msedge` / `chromium` |
| `COMPUTER_MONITOR` | 固定工作屏，默认 `primary`；可填 computer doctor 列出的显示器设备名，如 `\\.\DISPLAY2` |
| `COMPUTER_BROWSER_EXECUTABLE` | 显式浏览器 exe，例如 `C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe` |
| `COMPUTER_BROWSER_PROFILE_DIR` | 留空使用独立任务目录；填写专用 Agent 配置目录可保留登录状态 |
| `COMPUTER_DOWNLOAD_DIR` | 自行启动浏览器的下载目录；留空保存到任务产物的 downloads 目录 |
| `COMPUTER_CDP_ENDPOINT` | 可选本机已启动浏览器的 CDP URL，如 `http://127.0.0.1:9222` |

浏览器只在模型输出 `open_browser` 时启动或接入；`-Url` 为任务补充网页信息。个人浏览器登录状态不会自动继承。已打开的持久目录通过 CDP 接入，避免并发启动相同配置目录；接入实例的关闭和下载设置不归本任务管理。未接入的浏览器仍可纯视觉操作。

## 4. 跨应用真实模型验收与命令行任务

先填写实际服务参数，在可交互桌面上执行：

```bat
run.cmd -Mode computer-acceptance -Profile computer -Channel msedge -MaxSteps 30
```

验收依次执行 computer doctor、固定策略 computer smoke 和配置模型的本地跨应用任务。真实模型须从记事本任务出发，访问本地论文检索页，下载测试论文 PDF，再回到同一记事本记录并保存指定结果。独立 evaluator 核对下载文件、记事本内容及前台文档。总报告是 `artifacts/computer-acceptance/report.json`，环境检查在 `doctor.json`，任务证据在 `smoke/` 和 `computer-agent/`。

只有退出码 0、总报告 `status="passed"`、`success=true`、`real_model_verified=true`、`cross_application_verified=true`，且真实任务报告 `cross_application_verified=true`、独立 evaluator 的 `score=1.0`，才算跨应用模型跑通。缺参数为 `blocked`，调用或任务失败为 `failed`，均返回非零。安装、工程 smoke 或模拟 HTTP 成功不能代替真实模型阶段。

验收使用隔离的浏览器 profile、CDP 与下载目录，不使用 `.env` 中持久登录设置；不能给该命令显式传这些目录/接入参数。浏览器 channel 和自定义 exe 仍可按新电脑配置。

自定义任务在 **CMD** 中执行：

```bat
run.cmd -Profile computer -Task "读取当前记事本任务，按需打开网页，完成后回到记事本记录结果" -MaxSteps 30 -Output "artifacts\我的任务 01"
run.cmd -Profile computer -Task "完成当前桌面任务" -Channel chrome -BrowserExecutable "C:\Program Files\Google\Chrome\Application\chrome.exe" -BrowserProfileDir "D:\Agent 数据\Chrome Profile" -DownloadDir "D:\Agent 数据\下载" -MaxSteps 30
```

PowerShell 在命令前加 `.\`。可用 `-Model`、`-ApiUrl`、`-GroundType` 覆盖本次模型设置，`-TrustEnv` 允许模型请求使用系统代理。computer 任务必须提供 `-Task`，不支持 `-Headless`。默认输出在 `artifacts/computer-agent/<时间-标识>/`；自定义任务没有独立 evaluator 时 `score=null`，完成结论须结合用户指定的结果证据。

双屏默认只截取、操作主屏。已有任务窗口须完整移入工作屏并设为前台；Agent 新开的浏览器自动放到该屏。先用 `run.cmd -Mode doctor -Profile computer` 查看设备名与物理矩形，再用 `-Monitor "\\.\DISPLAY2"` 覆盖本次工作屏（底层 Python 参数为 `--monitor`）。doctor、smoke 与 acceptance 使用相同选屏设置；目标设备无效、消失或布局变化时拒绝继续。

例如在 CMD 指定 doctor 实际列出的副屏设备名：

```bat
run.cmd -Mode doctor -Profile computer -Monitor "\\.\DISPLAY2"
run.cmd -Mode computer-acceptance -Profile computer -Monitor "\\.\DISPLAY2" -Channel msedge -MaxSteps 30
run.cmd -Profile computer -Monitor "\\.\DISPLAY2" -Task "完成当前记事本任务" -MaxSteps 30 -Output "artifacts\副屏任务 01"
```

Agent 输出 Alt+Tab 动作时，执行层激活本任务最近合法确认、仍在工作屏内的其他窗口；不发送 Windows 全局 Alt+Tab。历史窗口仅供显式切换使用，自行抢前台仍会触发干扰处理。Win+R / Win+E 等快捷键打开的新应用窗口若无任务窗口所属关系，不会自动获得授权，可能被恢复到旧窗口；新应用建议通过工作屏任务栏点击启动或选择。

抢前台后，每次任务最多自动恢复一次最近有效任务窗口并重新截图；恢复失败或已经自动恢复一次后又被抢时安全停止，保存轨迹和 `report.json`，退出码非零。首次自动恢复前短暂离开又返回会丢弃旧观察和动作并重新观察，不因此直接停止。报告中 `safety_stop=true`、`termination_reason="safety_stop"`，原因保存在 `safety_stop_detail`。人工恢复窗口后重新启动任务，不支持断点续跑。运行期间不要在副屏并行使用鼠标键盘；输入检查无法消除系统竞争，需要继续副屏交互时使用独立虚拟机或独立交互会话。细节见 [工作屏与前台保护](docs/WINDOWS_COMPUTER_USE.md#工作屏与前台保护)。

底层入口 `python -m osworld_agent.run_computer_windows --help` 不读取 `.env`；使用包装器可自动加载配置和检查环境。`config.yaml` / `python -m osworld_agent.main` 仍为旧 OSWorld 配置与占位入口。

## 5. 浏览器专项入口

单独网页任务可继续使用 `browser` profile。它与通用 GUI 的按需浏览器配置不同：`OSWORLD_BROWSER_CHANNEL=auto` 依次尝试 Playwright Chromium、Edge、Chrome；可以 Headless。

```bat
setup.cmd -Profile browser -SkipSmoke
run.cmd -Mode smoke -Profile browser
run.cmd -Mode acceptance -Profile browser
run.cmd -Profile browser -Url "https://example.com" -Task "查看页面并返回标题" -MaxSteps 20 -Output "artifacts\browser-task"
```

浏览器专项真实验收写入 `artifacts/acceptance/report.json`，使用 Acme Ltd / Enterprise / 通知开启的本地表单及独立 evaluator。没有定位 URL 时只可执行确定的 DOM/AX 动作，视觉定位或歧义目标会失败。此验收不能证明 Windows 普通应用的视觉操作通过。

## 6. VMware / OSWorld 附录

Windows 主机通用 GUI 不需要 VMware。只有运行 OSWorld 客户机任务时，单独准备 VMware Workstation、与原机同一提交的外部 OSWorld（含 `desktop_env/`）、任务 JSON 和完整 VM 目录（含磁盘、快照链）。先正常关闭 VM 再迁移，不能只复制 `.vmx`。当前适配器控制 Ubuntu 客户机，客户机终端仍使用 Linux 协议。

根目录 `.env` 示例：

```dotenv
OSWORLD_DESKTOP_ENV_PATH=external/OSWorld
OSWORLD_EXAMPLES_DIR=external/OSWorld/evaluation_examples/examples
OSWORLD_VM_PATH=external/OSWorld/vmware_vm_data/Ubuntu0/Ubuntu0.vmx
OSWORLD_SNAPSHOT_NAME=init_state
OSWORLD_MODEL_PRESETS=config/model_presets.local.yaml
AGENTS_RESULTS_DIR=artifacts/viz
AGENTS_VIZ_PORT=8088
```

路径和快照名按新机器实际情况填写；记录原机外部仓库的远端与提交。先检查：

```bat
setup.cmd -Profile desktop -SkipSmoke
run.cmd -Mode doctor -Profile desktop
run.cmd -Mode vm -Profile desktop -TaskId "实际任务ID" -Domain chrome -MaxSteps 20
run.cmd -Mode viz -Profile desktop
```

VM CLI 使用 `.env` 的 `PLAN_*` / `GROUNDING_*`；Web GUI 使用忽略的 `config/model_presets.local.yaml`，须填写新机器可访问的模型 ID、URL、Key 与定位 `type`。VM 任务会回退到配置快照，执行前保留客户机未保存的工作。GUI 在 `http://127.0.0.1:8088`，Ctrl+C 停止服务。

外部依赖通过锁文件约束下的 `uv pip install` 安装；冲突时停止，使用该外部版本适用的 Windows 清单并配置 `OSWORLD_DESKTOP_REQUIREMENTS`。安装器不会安装 VMware、迁移 VM 或部署模型。更多准备细节见 [本地环境搭建指南](docs/本地环境搭建指南.md)。

## 7. 排查与更新

更新主线时先确认工作区干净且当前跟踪 `main`；固定标签 checkout 不直接 `git pull`。保留已有修改，再切换需要的版本。

```bat
run.cmd -Mode doctor -Profile computer
run.cmd -Mode doctor -Profile computer -CheckApi
git pull --ff-only
setup.cmd -Profile computer -SkipSmoke
```

`-CheckApi` 只发决策服务的 `GET /models`，不能验证推理、定位能力或任务效果；不提供该接口的服务需以实际任务结果判断。Key 不应输出到报告。网络下载问题检查代理、证书与 VPN；包源可使用 `UV_DEFAULT_INDEX`，浏览器下载可使用 `HTTPS_PROXY`、`NODE_EXTRA_CA_CERTS`、`PLAYWRIGHT_DOWNLOAD_HOST`。

锁屏、远程会话断开、权限差异或无法取得前台焦点时，先恢复交互桌面再验收。浏览器结构失败应回退视觉；需要可用的视觉定位服务。`uv sync --locked` 报不一致时核对源码与锁文件来自同一版本，部署时不要自动重新解锁依赖。

脚本依据环境指纹检测锁文件/元数据变化，再安装后以 `uv run --locked --no-sync` 启动；`requirements-windows.lock.txt` 是旧 pip 环境历史记录。系统浏览器及外部 OSWorld 版本另行记录。安装机制参考 [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)、[Python 管理](https://docs.astral.sh/uv/guides/install-python/) 和 [Playwright 浏览器](https://playwright.dev/python/docs/browsers)。
