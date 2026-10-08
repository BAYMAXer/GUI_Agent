# 新 Windows 电脑上的 Agent 自动跑通指南

本文是执行文档。收到“读取本文并跑通 GUI Agent”后，依次执行以下阶段，修复已明确的环境问题，并用 JSON 报告判断结果。默认目标是内置本地网页：把 Customer name 填为 Acme Ltd、选择 Enterprise、启用通知、保存并核对结果。该任务只写入隔离浏览器会话的本地测试页面。

当前 Agent 支持 Windows 上的浏览器控制，以及 Windows 主机上的 VMware 客户机桌面控制。决策与定位模型通过兼容 OpenAI Chat Completions 的 API 调用；环境安装脚本不部署模型，也不要求本机 GPU。

## 0. 输入与代码位置

仓库为 `https://github.com/BAYMAXer/master.git`，默认分支 `main`。工作目录可以自选；以下示例使用 `%USERPROFILE%\source\osworld_agent`，路径可以包含中文和空格。不要依赖原电脑的盘符、用户名、Codex Python 路径或复制过来的 `.venv`。

本指南对应第二版 `v2.0.0`；原先已上传代码保留为第一版 `v1.0.0`。要固定本指南对应代码，在以下 clone 命令中加入 `--branch v2.0.0`；标签 checkout 是 detached HEAD，若要修改代码，先创建自己的工作分支。

执行真实模型任务必需的输入：

| 输入 | 本机配置位置 | 说明 |
| --- | --- | --- |
| 决策模型 ID | `.env` 的 `PLAN_MODEL` | 模型 API 实际提供的 ID，模型别名映射在 `config/model_registry.py` |
| 决策 API base URL | `.env` 的 `PLAN_API_URL` | 例如服务给出的 `https://host/v1`，不要附加 `/chat/completions` |
| 决策 Key | `.env` 的 `PLAN_API_KEY` | 来自用户或已有环境；未认证服务需要用户明确设置为 `EMPTY` |
| 决策协议扩展 | `.env` 的 `PLAN_THINKING_STYLE` | 通用接口填 `none`；服务明确支持时填 `vllm` 或 `dashscope` |
| 可选定位服务 | `.env` 的 `GROUNDING_MODEL/API_URL/API_KEY` | 缺少定位 URL 时允许 DOM/AX 唯一目标动作；视觉定位/节点消歧需要定位服务 |
| 定位协议 | `.env` 的 `GROUNDING_PROTOCOL` | `structured` 节点与视觉、`pixel` 像素点、`normalized` 归一化点；服务任意部署名建议显式指定 |

这些参数不能从任务描述可靠推断。若缺少输入，先完成无需模型的环境和固定动作验证，再只请求缺少的参数；不要编造模型名称、URL 或 Key。保留已有 `.env`，只填写必要字段。不要把真实 Key 写入仓库、命令行参数或总结中。

## 1. 获取代码

先检查 `git --version`。Git 不可用时可以使用已配置的 WinGet 安装 `Git.Git`，随后重新打开终端。账号权限通过 GitHub 正常登录解决，私有仓库使用有访问权限的账号。

CMD：

```bat
if not exist "%USERPROFILE%\source" mkdir "%USERPROFILE%\source"
git clone https://github.com/BAYMAXer/master.git "%USERPROFILE%\source\osworld_agent"
cd /d "%USERPROFILE%\source\osworld_agent"
git status --short --branch
git rev-parse HEAD
```

PowerShell：

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE\source" -Force | Out-Null
git clone https://github.com/BAYMAXer/master.git "$env:USERPROFILE\source\osworld_agent"
Set-Location -LiteralPath "$env:USERPROFILE\source\osworld_agent"
git status --short --branch
git rev-parse HEAD
```

若目标目录已是正确仓库，复用它；仅在工作区干净时用 `git pull --ff-only` 更新。保留未提交的用户修改。确认仓库根目录存在 `setup.cmd`、`run.cmd`、`uv.lock`、`.python-version`、`.env.example` 和本文件；缺少这些文件说明代码版本不完整，先解决版本问题。

## 2. uv 自动准备环境

Windows 11 x64 是已验证目标。使用 64 位 PowerShell 进程；ARM64 主机未作为本方案的验证目标。

CMD，在仓库根目录运行：

```bat
setup.cmd -SkipSmoke
```

PowerShell 的等效命令：

```powershell
.\setup.cmd -SkipSmoke
```

脚本执行以下操作：查找兼容 uv 或把固定版本安装到 `.tools/uv/`；下载 `.python-version` 中的 CPython；以 `uv sync --locked` 创建本仓库的隔离 `.venv`；安装源码包及匹配的 Playwright Chromium；创建 `.env` 和 `config/model_presets.local.yaml`，已有文件保持原内容；写入 `.venv/.osworld-uv-ready.json` 环境标记。

默认 uv 为 0.12.23，默认 Python 为 3.12.15。无需预装 Python、pip 或 uv，无需激活环境或修改系统执行策略。下载需要访问 Astral/GitHub、包源和 Playwright CDN。已有兼容 uv 可以直接使用；也可把 `OSWORLD_UV_EXE` 指向完整的 `uv.exe` 路径。

每条命令完成后检查退出码：CMD 用 `echo %ERRORLEVEL%`，PowerShell 用 `$LASTEXITCODE`。非零时先修复该阶段，不能继续宣称安装成功。CMD 包装器默认不执行 `pause`，适合自动执行；需要人工查看双击错误时可以设置 `OSWORLD_PAUSE_ON_ERROR=1`。

旧环境失效、不是 Python 3.12 或继承系统包时：

```bat
setup.cmd -RecreateVenv -SkipSmoke
```

这个操作在本仓库内把旧 `.venv` 移到唯一备份目录后重新创建；不删除旧环境，也不操作其他路径。只在确认该旧环境需要重建时执行。

## 3. 先验证 Windows 与浏览器能力

```bat
run.cmd -Mode doctor
run.cmd -Mode smoke
```

PowerShell 使用 `.\run.cmd`，其他参数相同。运行器从任意工作目录调用时会自动切换到所在仓库根目录。默认 `auto` 依次尝试 Chromium、已安装的 Edge、Chrome；报告记录实际浏览器。若需固定已安装的浏览器，可用 `-Channel msedge` 或 `-Channel chrome`。

必须读取并验证：

| 文件 | 通过条件 |
| --- | --- |
| `artifacts/doctor/report.json` | `success=true`；Windows x64、Python 3.12、导入、资源文件与浏览器检查通过 |
| `artifacts/windows-smoke/report.json` | `success=true`、`score=1.0`、`grounding_calls=0`；任务为固定动作策略 |
| `artifacts/windows-smoke/trajectory.json` | 存在真实截图/观察、动作和结果；策略来源为 `scripted_test_double` |

这一步不调用模型。固定动作策略通过只能证明浏览器与框架连通，不能作为真实模型跑通的证据。

## 4. 填写模型与定位配置

模型配置的主入口是仓库根目录 `.env`。按用户提供的信息填写：

```dotenv
PLAN_MODEL=实际决策模型ID
PLAN_API_URL=https://实际服务地址/v1
PLAN_API_KEY=用户提供的Key
PLAN_THINKING_STYLE=none
OSWORLD_BROWSER_CHANNEL=auto
```

以上是字段示例，不能原样当成可用服务。`.env` 支持 `NAME=value`、整行注释及带引号的字面值；不支持变量展开、行尾注释或多行值。当前进程已有的非空环境变量优先，新字段优先于 `config/environment_aliases.json` 里的兼容名称。遇到配置似乎不生效，检查继承的环境变量，但不要输出 Key。

定位服务可选配置：

```dotenv
GROUNDING_MODEL=实际定位模型ID
GROUNDING_API_URL=https://实际定位服务地址/v1
GROUNDING_API_KEY=用户提供的定位Key
GROUNDING_PROTOCOL=structured
GROUNDING_THINKING_STYLE=none
```

可与决策服务共享服务地址，但模型 ID、Key 和定位协议仍分别配置。通用接口建议从 `none` 思考扩展开始；只有服务确认支持才启用对应扩展。需要精确 tokenizer/processor 时，配置 `PLAN_TOKENIZER_PATH`、`PLAN_PROCESSOR_PATH`、`GROUNDING_TOKENIZER_PATH`、`GROUNDING_PROCESSOR_PATH`；相对路径从仓库根目录解析。

`config.yaml` 是旧 OSWorld 配置示例，`python -m osworld_agent.main` 的环境仍是占位入口；本次浏览器跑通不要使用这两个入口。新机器不用修改模型实现文件，只需 `.env` 填实际服务 ID；仅需改变注册别名时才编辑 `config/model_registry.py`。

可选连通检查：`run.cmd -Mode doctor -CheckApi` 只对决策服务发 `GET /models`。接口列出模型不等于视觉能力或任务执行成功。部分服务没有该接口；404 时核对服务协议，并以接下来的实际推理任务为最终判据。

## 5. 用真实模型完成默认 GUI 任务

```bat
run.cmd -Mode acceptance
```

该命令依次执行环境检查、固定动作表单、配置的真实模型表单，并保存分阶段报告。默认显示浏览器；自动执行时可用 `run.cmd -Mode acceptance -Headless`。若阶段 3 已单独跑过，重复的前置验证仍无模型费用。

必须同时满足：

- 命令退出码为 `0`。
- `artifacts/acceptance/report.json` 中 `status="passed"`、`success=true`、`real_model_verified=true`。
- `artifacts/acceptance/browser-agent/report.json` 中 `success=true`、`score=1.0`、`evaluation_available=true`、`reward_source="environment_evaluator"`。
- 对应轨迹和截图存在，表单确实保存了 Acme Ltd / Enterprise / 通知开启；不是只看到模型输出“完成”。

缺少模型参数时，前两阶段可完成，但最终报告为 `status="blocked"`，退出码非零，`real_model_verified=false`。请求必要输入后重新运行。模型调用或任务失败时为 `failed`；读取报告中的失败阶段与该阶段 stdout，针对原因修复，不能把它改成成功或换成固定策略充当真实模型。

## 6. 用户自己的任务：CMD 与 PowerShell

真实模型验收通过后，按用户给出的目标 URL 和任务运行；未给其他任务则默认表单通过即可完成本指南。

CMD 单行命令：

```bat
run.cmd -Url "https://example.com" -Task "查看页面并返回标题" -MaxSteps 20 -Output "artifacts/my-task"
run.cmd -Model "实际决策模型ID" -ApiUrl "https://实际服务地址/v1" -Task "用户任务" -Channel msedge
```

PowerShell：

```powershell
.\run.cmd -Url "https://example.com" -Task "查看页面并返回标题" -MaxSteps 20 -Output "artifacts/my-task"
```

Key 仍来自 `.env`。`-Model`、`-ApiUrl`、`-GroundType` 是本次调用的覆盖参数，不改写 `.env`。使用 HTTP 代理请求模型时加 `-TrustEnv`。不要给接受默认表单的 `-Mode acceptance` 追加自定义 `-Url` 或 `-Task`。

新浏览器会话不继承个人登录状态。用户任务需要登录时，先落实登录/已有 CDP 会话方案；底层入口支持 `--cdp-endpoint`，通过 `run_browser_windows.py --help` 查看。不要把单纯 API 连通当作已登录。

普通浏览器任务的输出为 `-Output` 指定目录里的 `report.json`、`trajectory.json`、`sft.jsonl`、`grounding-sft.jsonl` 及截图；任务失败返回非零退出码。自定义网页一般没有独立 evaluator，报告可能 `score=null`，此时需要用户定义的结果证据，不能声称获得基准满分。

## 7. 只有明确要求桌面/VMware 时才执行

浏览器默认任务不需要 VMware。桌面任务需额外准备 Windows 上的 VMware Workstation、与原环境同一提交的外部 OSWorld 源码（实际包含 `desktop_env/`）、任务 JSON 和整个 VM 文件夹（含磁盘及目标快照）。这些资料不包含在本仓库 clone 中。

在 `.env` 配置实际新电脑路径：

```dotenv
OSWORLD_DESKTOP_ENV_PATH=external/OSWorld
OSWORLD_EXAMPLES_DIR=external/OSWorld/evaluation_examples/examples
OSWORLD_VM_PATH=external/OSWorld/vmware_vm_data/Ubuntu0/Ubuntu0.vmx
OSWORLD_SNAPSHOT_NAME=init_state
```

快照名称按实际 VM 修改，例如 `init_state_ca3`。Windows 主机不意味着客户机一定是 Windows；当前 VMware 适配器控制 Ubuntu 客户机，因此客户机终端命令保持 Linux 协议是正确的。

```bat
setup.cmd -Profile desktop -SkipSmoke
run.cmd -Mode doctor -Profile desktop
run.cmd -Mode vm -TaskId "用户提供的实际任务ID" -Domain chrome -MaxSteps 20
```

纯 CLI 使用 `.env` 的 `PLAN_*`、`GROUNDING_*`，Key 不进入命令行。`vm` 任务开始会把客户机回退到配置的快照，这是 OSWorld 的任务初始化；保留 VM 中尚未保存的用户工作，再执行该模式。`AGENTS_RESULTS_DIR` 下生成本次任务的 `report.json`、`trajectory.json`、`traj.jsonl` 与截图。

若需要 Web GUI，使用 `run.cmd -Mode viz -Profile desktop`；网页模型候选取自 `config/model_presets.local.yaml`，需要在该文件分别填写服务名称、URL、Key 和 `type`（`structured/pixel/normalized`）。

桌面依赖通过 `uv pip install` 安装，并受当前 `uv.lock` 导出的约束限制；不允许悄悄改变已验证的核心依赖。外部依赖清单有平台冲突时，提供该外部提交对应的 Windows 依赖文件并设置 `OSWORLD_DESKTOP_REQUIREMENTS`。缺少外部代码/VM/快照或服务器访问条件时，报告缺失项，保留已通过的浏览器结果。

## 8. 有明确依据的故障处理

| 现象 | 下一步 |
| --- | --- |
| uv/Python/包下载失败 | 检查网络/VPN/代理；包源使用 `UV_DEFAULT_INDEX`；按本机证书配置修复 TLS，不跳过检查 |
| `Missing expected target directory for Python minor version link` | 脚本会尝试用固定补丁版本的真实可执行文件继续；若仍失败，用受支持的已安装 Python 3.12，通过 `setup.cmd -Python "C:\真实路径\python.exe" -SkipSmoke` 继续 uv 安装，保留原 uv 存储 |
| `.venv` 失效或继承系统包 | 使用本仓库的 `setup.cmd -RecreateVenv -SkipSmoke` 保留旧环境后重建 |
| `uv sync --locked` 提示 lock 不一致 | 检查代码、`pyproject.toml` 和 `uv.lock` 是否来自同一版本；部署时不要自动重新解锁依赖 |
| Chromium 无法启动 | `auto` 会尝试 Edge/Chrome；已安装时可指定 `-Channel msedge`，以实际自检结果为准 |
| 401/403 或模型不存在 | 只核对实际 base URL、ID、Key 和权限，不改变模型行为代码，不在报告中打印 Key |
| 决策动作需要定位但没有服务 | 补齐实际定位 URL/Key/模型及协议；保留失败证据，不编造节点或坐标 |
| VM/快照检查失败 | 核对 `.vmx`、完整磁盘链、`OSWORLD_SNAPSHOT_NAME` 与 `vmrun listSnapshots`；不要只复制 `.vmx` |
| 命令行返回 0，但页面任务没有实际效果 | 对照独立 evaluator、保存状态和截图；本项目任务失败应返回非零，不能只依据模型文字判断 |

uv 的 Windows 版本链接问题有 [上游问题记录](https://github.com/astral-sh/uv/issues/19622)。安装/依赖行为参考 [uv 安装](https://docs.astral.sh/uv/getting-started/installation/)、[Python 管理](https://docs.astral.sh/uv/guides/install-python/) 与 [锁定同步](https://docs.astral.sh/uv/concepts/projects/sync/)；浏览器行为参考 [Playwright 浏览器文档](https://playwright.dev/python/docs/browsers)。

## 9. 完成后向用户交付

简要说明代码提交、uv/Python/实际浏览器版本、通过了哪些阶段、真实模型是否通过独立 evaluator、用户任务实际结果以及报告/轨迹的本机绝对路径。若仍缺少参数或 VM 条件，只报告已验证的部分和准确缺失项。不要给出未经执行的“已跑通”结论。

可直接给新电脑上的 Agent 这段任务：

> 完整读取仓库中的 `docs/WINDOWS_AGENT_RUNBOOK.md`，按文档完成 uv 环境、Windows 浏览器自检及真实模型默认表单验收。复用已有配置并保留我的代码修改；缺少模型参数时先完成无需模型的阶段，再询问必要输入。以退出码、独立 evaluator、JSON 报告和轨迹截图判断完成，最后给出证据路径。
