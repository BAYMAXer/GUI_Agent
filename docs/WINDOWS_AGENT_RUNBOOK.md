# 新 Windows 电脑上的 Agent 自动跑通指南

本文是执行文档。收到“读取本文并跑通 GUI Agent”后，按顺序准备 uv 环境，验证 Windows 主机的交互能力，再用真实模型完成本地跨应用任务；用退出码、独立 evaluator 和产物判断结果。

当前主线是 `computer` profile：一个任务在记事本、浏览器和普通应用之间连续执行。每步刷新桌面截图；仅已接入的 Chrome/Edge 网页处于前台且网页获输入焦点时提供精简 AX。地址栏、原生对话框、其他应用、未接入浏览器实例和结构失败时恢复视觉操作。浏览器按模型的 `open_browser` 动作启动，任务结束仅关闭自身创建的实例。普通应用不读取桌面 AX 或本地文档内容替代视觉观察。

脚本默认 profile 仍为 `browser`，本文主线必须显式传 `-Profile computer`。浏览器专项表单与 VMware 客户机运行见附录，不可代替跨应用验收。安装不会部署模型、安装 VMware 或迁移 VM。模型经 HTTP API 调用，不要求本机 GPU。

## 0. 所需输入与执行条件

仓库是 `https://github.com/BAYMAXer/GUI_Agent.git`，默认分支 `main`；第一版 `v1.0.0`、第二版 `v2.0.0` 保留，本指南对应第三版 `v3.0.0`。标签 checkout 为 detached HEAD，需要修改时先创建自己的工作分支。

需要 Windows x64、64 位 PowerShell、已登录且已解锁的交互桌面，以及与目标应用相同的权限级别。不要在锁屏、断开的远程会话、Windows 服务或无人值守会话里执行 computer smoke/任务；computer 不支持 Headless。纯视觉任务不要求浏览器；本指南跨应用验收需可用的 Chrome/Edge，或明确选择安装 Playwright Chromium。先保存用户其他应用的工作，测试期间不要抢夺焦点或改变桌面布局。

真实任务参数如下：

| 输入 | 配置位置 | 要求 |
| --- | --- | --- |
| 决策模型 ID | `.env` 的 `PLAN_MODEL` | 服务实际提供的模型 ID；别名在 `config/model_registry.py` |
| 决策 API base URL | `PLAN_API_URL` | 服务实际给出的 `https://host/v1` 等 base URL，不附加 `/chat/completions` |
| 决策 Key | `PLAN_API_KEY` | 用户或现有环境提供；未认证服务需明确填写 `EMPTY` |
| 决策扩展协议 | `PLAN_THINKING_STYLE` | 通用接口 `none`；实际支持时才用 `vllm` / `dashscope` |
| 可选独立定位服务 | `GROUNDING_MODEL/API_URL/API_KEY` | 填写独立 URL 时须有相应模型 ID 和 Key |
| 定位协议 | `GROUNDING_PROTOCOL` | 独立服务用实际协议 `structured` / `pixel` / `normalized` |
| 本机浏览器设置 | `COMPUTER_BROWSER_*` / `COMPUTER_DOWNLOAD_DIR` / `COMPUTER_CDP_ENDPOINT` | 路径、专用登录配置及本机 CDP，按新电脑实际情况填写 |

跨普通应用任务必须有能完成视觉定位的服务能力。未配置 `GROUNDING_API_URL` 时，computer 复用 PLAN 服务；PLAN 服务必须同时支持图片规划与所选定位协议，否则补齐独立服务。`GROUNDING_PROTOCOL` / `-GroundType` 也适用于复用，`auto` 按注册表选择，通常为 `structured`。不能因为网页唯一 AX 目标可操作，就声称普通应用定位也已验证。

不要推测模型 ID、URL、Key 或旧机器路径。缺少服务参数时先完成无需模型的安装、doctor 和固定策略 smoke，最后只请求缺失输入。已有 `.env` 保留，只更新必要字段；真实 Key 不进入仓库、命令行参数、截图说明或最终报告。

## 1. 获取代码并记录版本

先检查 `git --version`。Git 不可用时可用 WinGet 安装 `Git.Git` 并重新打开终端。私有仓库使用有访问权限的 GitHub 登录账号，不把账户密码或 token 放进远端 URL。

**CMD**，中文和空格路径示例：

```bat
if not exist "%USERPROFILE%\source" mkdir "%USERPROFILE%\source"
git clone --branch v3.0.0 https://github.com/BAYMAXer/GUI_Agent.git "%USERPROFILE%\source\GUI Agent 移植"
cd /d "%USERPROFILE%\source\GUI Agent 移植"
git status --short --branch
git rev-parse HEAD
```

**PowerShell**：

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE\source" -Force | Out-Null
git clone --branch v3.0.0 https://github.com/BAYMAXer/GUI_Agent.git "$env:USERPROFILE\source\GUI Agent 移植"
Set-Location -LiteralPath "$env:USERPROFILE\source\GUI Agent 移植"
git status --short --branch
git rev-parse HEAD
```

若已有正确仓库则复用；工作区干净且当前跟踪 `main` 时可用 `git pull --ff-only`。保留未提交修改，不强制 checkout 或 reset。确认根目录存在 `setup.cmd`、`run.cmd`、`run_computer_windows.py`、`uv.lock`、`.python-version`、`.env.example` 和本文件。不要复制原电脑的 `.venv`，也不要依赖原机盘符、用户名或 Codex Python 路径。

## 2. uv 一键准备环境

在仓库根目录执行：

```bat
setup.cmd -Profile computer -SkipSmoke
```

PowerShell 使用 `.\setup.cmd -Profile computer -SkipSmoke`。脚本查找兼容 uv 或安装固定版本到 `.tools/uv/`，下载固定 CPython，用 `uv sync --locked` 创建隔离 `.venv` 并安装源码包与依赖，创建缺失的 `.env` 和本机预设，运行 computer doctor 并写入环境标记。computer 默认不下载浏览器，不注入输入；仅明确配置 `COMPUTER_BROWSER_CHANNEL=chromium` 且没有自定义 exe/CDP 时下载匹配的 Chromium。已有 Chrome/Edge 可直接使用。

当前 `.uv-version` 是 0.12.23，`.python-version` 是 3.12.15；无需预装 Python、pip 或 uv，不用激活环境或更改系统执行策略。已有兼容 uv 可复用；`OSWORLD_UV_EXE` 可指定完整 exe 路径。网络需能访问 Astral/GitHub、包源和 Playwright 下载源。已有 Python 3.12 x64 时可用：

```bat
setup.cmd -Profile computer -Python "C:\实际 Python 路径\python.exe" -SkipSmoke
```

每条命令检查退出码：CMD 用 `echo %ERRORLEVEL%`，PowerShell 用 `$LASTEXITCODE`。非零时修复该阶段，不能宣称安装成功。CMD 包装器不默认 pause；人工双击查看失败时可设置 `OSWORLD_PAUSE_ON_ERROR=1`。

旧环境损坏、Python 版本不符或继承系统包时才使用：

```bat
setup.cmd -Profile computer -RecreateVenv -SkipSmoke
```

它仅在本仓库中把旧 `.venv` 移到唯一备份目录后重建，不删除旧环境或改动其他路径。部署时不重新解锁依赖，源码与 `uv.lock` 须来自同一版本。

## 3. 验证 Windows 与工程链路

```bat
run.cmd -Mode doctor -Profile computer
run.cmd -Mode computer-smoke -Profile computer -Channel msedge
```

PowerShell 在 `run.cmd` 前加 `.\`。运行器从其他工作目录调用也会切换到自身仓库根目录。指定 `-Channel chrome` 可验证 Chrome；computer 的 `auto` 尝试 Chrome、Edge、再已安装的 Playwright Chromium。自定义 exe 可经 `COMPUTER_BROWSER_EXECUTABLE` 或 `-BrowserExecutable` 配置 smoke。computer doctor 检查资源导入、截图和系统前台焦点，不启动浏览器，不注入输入；缺少可选浏览器不阻断纯视觉模式。

`computer-smoke` 会打开自身测试记事本文档和本地论文页，在同一 Agent 内下载 PDF、返回记事本中文回写并保存。它另外检查地址栏、文件对话框、后台及另一浏览器实例、AX 失败、旧结构引用和焦点切换的路由；仅关闭自身测试文档与浏览器。固定策略和坐标定位是替身，不请求真实模型。

必须读取报告：

| 文件 | 通过条件 |
| --- | --- |
| `artifacts/doctor/report.json` | `success=true`、`computer_checked=true`；交互桌面检查通过 |
| `artifacts/computer-smoke/report.json` | `success=true`、`score=1.0`、所有 checks 通过；`model_verified=false` |
| `artifacts/computer-smoke/trajectory.json` | 统一 v3 观察、截图、动作与结果；`policy=scripted_test_double`、`use_for_model_training=false` |

每次 fixture 和下载目录是独立新目录，避免旧文件误通过新任务。smoke 验证框架和焦点路由，不能证明真实模型准确率或任务成功，也不能把替身轨迹用于模型训练。

## 4. 填写模型与浏览器配置

主入口是根目录 `.env`，按实际服务信息填写：

```dotenv
PLAN_MODEL=实际决策模型ID
PLAN_API_URL=https://实际服务地址/v1
PLAN_API_KEY=用户提供的Key
PLAN_THINKING_STYLE=none

COMPUTER_BROWSER_CHANNEL=msedge
COMPUTER_BROWSER_EXECUTABLE=
COMPUTER_BROWSER_PROFILE_DIR=
COMPUTER_DOWNLOAD_DIR=
COMPUTER_CDP_ENDPOINT=
```

以上是字段示例，不是可用端点。终端已有非空环境变量优先，新字段优先于 `config/environment_aliases.json` 中旧别名；配置不生效时检查继承值，不输出 Key。`.env` 是字面值 `NAME=value`，支持引号和整行注释，不支持变量展开、行尾注释或多行值。相对路径以仓库根目录解析。

需要独立定位时填写：

```dotenv
GROUNDING_MODEL=实际定位模型ID
GROUNDING_API_URL=https://实际定位服务地址/v1
GROUNDING_API_KEY=用户提供的定位Key
GROUNDING_PROTOCOL=structured
GROUNDING_THINKING_STYLE=none
```

未配置独立 URL 时 computer 复用 PLAN 服务，并同样读取 `GROUNDING_PROTOCOL` / `-GroundType`。协议可为 `structured` 节点与视觉、`pixel` 像素点或 `normalized` 归一化点；`auto` 根据注册表选择，任意部署名称建议显式填写实际协议。思考扩展先按服务设置，通用接口通常使用 `none`。精确 token 统计可另填 `PLAN_TOKENIZER_PATH`、`PLAN_PROCESSOR_PATH`、`GROUNDING_TOKENIZER_PATH`、`GROUNDING_PROCESSOR_PATH`，相对路径从仓库根目录计算。

浏览器只按需启动。可把 `COMPUTER_BROWSER_EXECUTABLE` 指向新机实际 Chrome/Edge exe；例如 `C:\Program Files\Google\Chrome\Application\chrome.exe`。`COMPUTER_BROWSER_PROFILE_DIR` 留空使用任务独立目录，指定专用 Agent 目录可保留登录；`COMPUTER_DOWNLOAD_DIR` 留空使用产物目录下 downloads。不要把个人浏览器配置目录直接当 Agent 目录。

`COMPUTER_CDP_ENDPOINT` 可接入已启动的本机 CDP 浏览器，如 `http://127.0.0.1:9222`。仅匹配的前台网页提供 AX；任务不关闭接入实例、不改其下载设置。已占用的持久目录应通过 CDP 接入，避免第二次启动。验收采用独立 fixture/browser profile/downloads，不依赖个人登录或旧下载；用户任务再应用持久路径配置。

`config.yaml` / `python -m osworld_agent.main` 是旧 OSWorld 配置和占位入口，当前 computer 使用 `run.cmd -Profile computer`。一般只修改 `.env`，无需改模型实现；只有调整注册别名才编辑 `config/model_registry.py`。

可选 `run.cmd -Mode doctor -Profile computer -CheckApi` 只请求决策服务的 `GET /models`，不能证明图像输入、定位能力或推理任务通过；没有该接口的服务须以实际任务验收判断。

## 5. 真实模型跨应用验收

在已解锁的交互桌面执行，不加 `-Headless`：

```bat
run.cmd -Mode computer-acceptance -Profile computer -Channel msedge -MaxSteps 30
```

该命令分阶段执行 computer doctor、固定策略 smoke 和真实模型跨应用本地任务。环境报告在 `artifacts/computer-acceptance/doctor.json`。真实任务从记事本任务指令出发，检索本地测试页中的 GUI Agent Browser Study，下载 PDF，然后回到同一记事本文档，把全文替换为以下指定结果并保存：

```text
已下载 GUI Agent Browser Study 论文 PDF。跨应用任务验证完成。
```

独立 evaluator 要求本次 `agent-study.pdf` 的字节与生成的测试 PDF 一致、记事本保存全文与上述行一致，而且前台仍是该 fixture 的记事本文档。轨迹还须有桌面→网页→桌面。fixture 每次使用随机独立目录，路径从真实任务报告的 `fixture_directory` 获取。它不使用用户自己的文档或真实网站；验收验证目标机器上的跨应用工程和模型链路。

必须同时满足：

- 命令退出码 `0`。
- `artifacts/computer-acceptance/report.json` 中 `status="passed"`、`success=true`、`real_model_verified=true`、`cross_application_verified=true`。
- `artifacts/computer-acceptance/computer-agent/report.json` 中 `success=true`、`score=1.0`、`evaluation_available=true`、`reward_source="environment_evaluator"`。
- 真实任务报告 `cross_application_verified=true`，证明确实经同一任务连续跨应用，而不是只生成结果文件。
- 独立 evaluator 已核对本次下载论文文件和记事本保存结果；对应截图与轨迹含真实模型调用及跨应用动作。
- `artifacts/computer-acceptance/smoke/report.json` 通过，但仍标记替身策略和 `model_verified=false`。

环境与工程阶段通过后，缺少服务输入时报告 `blocked`、退出码非零、`real_model_verified=false`；先保留独立可完成的阶段证据，再请求缺失参数。前置环境故障、模型调用、定位或任务结果失败为 `failed`。不能把它改为 passed，不能替换成固定策略或模拟 HTTP 响应充当真实模型，也不能只凭模型输出“完成”判断。真实服务可产生推理费用，使用用户已提供或授权的服务配置。核对配置确实指向所需的真实模型服务，不能因模拟服务器也生成相同 JSON 就宣布模型跑通。

不要给 computer-acceptance 追加自定义 `-Task`、`-Url`、`-BrowserProfileDir`、`-CdpEndpoint` 或 `-DownloadDir`。它使用有独立结果核验的固定任务与隔离会话，忽略 `.env` 中的持久 profile/CDP/download 设置；显式传这些参数会报错。channel 和自定义 exe 仍可配置；用户目标及持久登录另用下一阶段运行。

## 6. 运行用户任务

任务从当前桌面开始；模型看到截图而不是通过本地文件读取获取记事本内容。用户任务必须显式 `-Task`。`-Url` 只补充相关网页信息，浏览器仍由模型 `open_browser` 按需创建或接入。

CMD：

```bat
run.cmd -Profile computer -Task "读取当前记事本任务，按需打开网页，完成后回到记事本记录结果" -MaxSteps 30 -Output "artifacts\我的任务 01"
run.cmd -Profile computer -Task "完成当前桌面任务" -Channel chrome -BrowserExecutable "C:\Program Files\Google\Chrome\Application\chrome.exe" -BrowserProfileDir "D:\Agent 数据\Chrome Profile" -DownloadDir "D:\Agent 数据\下载" -MaxSteps 30
```

PowerShell：

```powershell
.\run.cmd -Profile computer -Task "查看当前桌面任务，在相关网页查询后回写到记事本" -Url "https://example.com" -MaxSteps 30 -Output "artifacts\我的任务 01"
```

Key 由 `.env` 加载。`-Model`、`-ApiUrl`、`-GroundType` 只覆盖本次调用，`-TrustEnv` 允许模型 API 使用环境代理；浏览器参数可通过 `.env` 或 `-BrowserExecutable`、`-BrowserProfileDir`、`-DownloadDir`、`-CdpEndpoint` 覆盖。

普通任务默认生成 `artifacts/computer-agent/<时间-标识>/`，包含 report、trajectory、decision SFT、grounding SFT 和截图。任务失败返回非零；没有独立 evaluator 时 `score=null`，需要用户目标定义的文件、页面状态或其他独立结果证据，不能声称基准满分。

## 7. 浏览器专项附录

仅需网页控制时可用保留的 `browser` profile，允许 Headless；按以下专项命令执行，不据此宣称跨普通应用已验证：

```bat
setup.cmd -Profile browser -SkipSmoke
run.cmd -Mode doctor -Profile browser
run.cmd -Mode smoke -Profile browser
run.cmd -Mode acceptance -Profile browser
run.cmd -Profile browser -Url "https://example.com" -Task "查看页面并返回标题" -MaxSteps 20 -Output "artifacts\browser-task"
```

专项模式的 `OSWORLD_BROWSER_CHANNEL=auto` 顺序是 Playwright Chromium、Edge、Chrome，与 computer 的优先级不同。没有 grounding URL 时只支持可确定的 DOM/AX 目标；视觉定位和歧义目标仍需服务。

浏览器 acceptance 使用本地表单 Acme Ltd / Enterprise / 通知开启，报告在 `artifacts/acceptance/report.json`；要求真实任务 score=1、独立 evaluator、success 与 real_model_verified 都通过。该报告和 computer-acceptance 是不同验收。网页登录不自动继承；专项已有 CDP 入口通过 `run_browser_windows --help` 查看，底层 Python 不自行读取 `.env`。

## 8. VMware / OSWorld 附录

Windows 主机 computer 任务不需要 VMware。仅明确要求 OSWorld 客户机任务时，额外准备 VMware Workstation、同一提交的外部 OSWorld（含 `desktop_env/`）、任务 JSON 和整个 VM 文件夹（磁盘及目标快照链）。记录原机外部仓库远端与提交，在新机从相同来源恢复；正常关闭 VM 后迁移，不能只复制 `.vmx`。

```dotenv
OSWORLD_DESKTOP_ENV_PATH=external/OSWorld
OSWORLD_EXAMPLES_DIR=external/OSWorld/evaluation_examples/examples
OSWORLD_VM_PATH=external/OSWorld/vmware_vm_data/Ubuntu0/Ubuntu0.vmx
OSWORLD_SNAPSHOT_NAME=init_state
```

路径、快照名称按实际环境填写。Windows 主机不表示客户机是 Windows；当前适配器控制 Ubuntu 客户机，客户机终端命令保持 Linux 协议。

```bat
setup.cmd -Profile desktop -SkipSmoke
run.cmd -Mode doctor -Profile desktop
run.cmd -Mode vm -Profile desktop -TaskId "用户提供的实际任务ID" -Domain chrome -MaxSteps 20
run.cmd -Mode viz -Profile desktop
```

VM CLI 读取 `.env` 的 `PLAN_*` / `GROUNDING_*`；Web GUI 使用 `config/model_presets.local.yaml`，分别填写模型 ID、URL、Key、定位 type。VM 任务启动回退至配置快照，事先保留客户机未保存的工作。`AGENTS_RESULTS_DIR` 保存任务报告、轨迹和截图。

外部依赖用锁文件约束安装，平台冲突时停止；可设置 `OSWORLD_DESKTOP_REQUIREMENTS` 为该外部版本的 Windows 清单，不能悄悄改变核心依赖或换外部版本。clone 不包含 VM、外部源码或服务部署；缺条件时报告缺失项并保留已通过的主机结果。更多路径与 VM 准备见 [本地环境搭建指南](本地环境搭建指南.md)。

## 9. 故障处理与最终交付

| 现象 | 下一步 |
| --- | --- |
| uv/Python/依赖下载失败 | 检查网络、VPN、代理、证书；包源 `UV_DEFAULT_INDEX`，不跳过 TLS 检查 |
| `Missing expected target directory for Python minor version link` | 脚本尝试固定补丁版本真实 exe；仍失败用本机 Python 3.12 x64 的 `-Python` 安装，保留 uv 存储 |
| 锁文件不一致或旧 venv 不适用 | 检查源码与锁版本；必要时 `setup.cmd -Profile computer -RecreateVenv -SkipSmoke` 保留旧环境重建 |
| 截图、前台焦点或输入不可用 | 解锁并恢复交互桌面，检查会话与权限一致，避免焦点被其他操作抢夺 |
| Chrome/Edge 无法启动 | 核对 channel、实际 exe、企业远程调试策略、专用 profile 是否被占用；尝试实际已安装 channel |
| 网页没有 AX / 进入地址栏或对话框 | 视觉回退是预期行为；核对定位服务能根据实际桌面截图操作，不注入后台 AX |
| 401/403 / 模型不存在 / 定位响应不兼容 | 核对实际 ID、base URL、Key、权限与定位协议；不输出 Key，不编造坐标 |
| `score=null` 或模型称完成但文件不符 | 读取独立结果证据；自定义任务不能据此声称基准满分 |
| VM / 快照失败 | 核对完整 VM、快照名、vmrun 和外部源码，不把浏览器结果当 VM 通过 |

浏览器下载可使用 `HTTPS_PROXY`、`NODE_EXTRA_CA_CERTS`、`PLAYWRIGHT_DOWNLOAD_HOST`。uv Windows 版本链接问题见 [上游记录](https://github.com/astral-sh/uv/issues/19622)；机制参考 [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)、[Python 管理](https://docs.astral.sh/uv/guides/install-python/)、[锁定同步](https://docs.astral.sh/uv/concepts/projects/sync/) 和 [Playwright 浏览器](https://playwright.dev/python/docs/browsers)。

交付时说明提交/标签、uv/Python/实际浏览器版本、每一阶段结果、真实模型是否经独立 evaluator 验证、用户任务结果和本机报告/轨迹绝对路径。缺参数时准确列出缺失项，不给未经执行的“已跑通”结论。

可直接交给新电脑 Agent：

> 完整读取 `docs/WINDOWS_AGENT_RUNBOOK.md`，按 `computer` 主线完成 uv 环境、Windows 交互桌面自检、固定策略跨应用 smoke 和真实模型 computer-acceptance。复用已有配置并保留代码修改；缺模型参数时先执行无需模型的阶段，再请求必要输入。以退出码、独立下载文件/记事本结果核验、JSON 报告和轨迹截图判断完成，最后给出证据路径。浏览器专项和 VMware 只有我明确需要时再执行。
