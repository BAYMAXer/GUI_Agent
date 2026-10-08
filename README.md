# osworld_agent：Windows 克隆后安装并运行

本仓库包含 Agent 框架、浏览器适配器和 VMware/OSWorld 适配器。**本地运行指 Agent 在 Windows 上运行；决策模型和视觉定位模型通过 HTTP API 调用。** 不需要本机 GPU。模型服务需要另行部署或使用已开通的服务，并保证新电脑能访问它。

| 运行方式 | 操作对象 | clone 后还需什么 |
| --- | --- | --- |
| 浏览器模式 | Windows 上由 Agent 启动的 Chromium 网页 | 模型 URL、模型名称、API Key；安装脚本自动准备 Python、依赖、Chromium |
| 完整桌面模式 | VMware 虚拟机内的桌面与应用 | VMware、外部 `desktop_env` 源码与任务集、整个 VM 文件夹和 `init_state` 快照、决策与定位模型端点 |

浏览器模式可以独立运行。当前完整桌面模式控制 VMware 客户机；本仓库没有直接接管 Windows 主机上任意应用的运行入口。

## 1. 代码仓库与后续更新

代码仓库：[BAYMAXer/master](https://github.com/BAYMAXer/master)，默认分支为 `main`。原电脑或已有 clone 的电脑修改代码后，在仓库目录执行：

```powershell
git add .
git status --short
git commit -m "Update agent"
git push origin main
```

第一次 push 使用 Git 的 GitHub 登录流程；不要使用账户密码或把 token 写进仓库 URL。若 `git commit` 提示缺少身份，按提示配置自己的 `user.name` 和 `user.email` 后重试。

`.gitignore` 已排除 `.venv`、`.env`、本机配置、运行轨迹、外部 OSWorld 目录及 VMware 文件。上传前检查 `git status --short` 和 `git diff --cached`。已有 `config.yaml` / `config/model_presets.yaml` 包含原环境的模型地址，公开仓库前按需要替换；真实 Key 放 `.env` 或忽略的 `config/model_presets.local.yaml`。

不要上传或复制 `.venv` 到另一台电脑：Python 虚拟环境包含绝对路径，新电脑需要重新创建。

## 2. 新 Windows 电脑：clone + 一键自检

建议 Windows 11 64 位。需要网络访问 GitHub、Python 包源和 Playwright 浏览器下载源。Git 尚未安装时，在 PowerShell 执行后重新打开终端：

```powershell
winget install --id Git.Git --exact --source winget --accept-package-agreements --accept-source-agreements
```

然后执行：

```powershell
git clone https://github.com/BAYMAXer/master.git osworld_agent
cd osworld_agent
.\run.cmd -Mode smoke
```

首次运行会自动执行安装：检测/安装 Python 3.12（64 位）、创建隔离 `.venv`、安装本项目、下载匹配的 Chromium、创建本地配置，再运行浏览器任务自检。目录名可修改，可包含空格。无需激活 venv，也无需修改系统 PowerShell 执行策略。

自检使用固定动作策略填写本地表单，不调用模型、不需要 Key。成功时报告包含 `success: true`、`score: 1.0`、`grounding_calls: 0`，结果在 `artifacts/windows-smoke/`。**自检验证安装、浏览器、动作执行和轨迹导出；不代表模型任务成功率。**

也可双击 `setup.cmd` 单独安装。WinGet 不可用时先从 [Python 官网](https://www.python.org/downloads/windows/) 安装 Python 3.12 的 64 位版本，再运行 `setup.cmd`。指定解释器：

```powershell
.\setup.cmd -Python "C:\path\to\Python312\python.exe"
```

## 3. 填一次配置，以后一键运行真实 Agent

编辑安装时生成的 `.env`，填写：

```dotenv
QWEN_MODEL=你的模型服务实际提供的模型ID
QWEN_API_URL=https://你的模型服务器/v1
QWEN_API_KEY=你的Key
QWEN_THINKING_STYLE=none
OSWORLD_BROWSER_CHANNEL=auto
```

模型应兼容 OpenAI Chat Completions 和图片输入。这里的 `QWEN_` 是现有入口的变量名，也可以填其他兼容服务。URL 填 API base URL，不填完整的 `/chat/completions`。未认证的内网端点明确填写 `QWEN_API_KEY=EMPTY`。远程内网模型需要连接同一网络/VPN。`QWEN_THINKING_STYLE` 可选 `none`、`vllm`、`dashscope`，按服务协议设置。

`.env` 采用 `NAME=value`，支持带引号的字面值和整行 `#` 注释，不支持变量展开、行尾注释或多行值。当前终端中已有的非空环境变量优先于 `.env`。

双击 `run.cmd` 即可显示浏览器并运行默认本地表单任务；也可在 PowerShell 指定自己的网页任务：

```powershell
.\run.cmd -Url "https://example.com" -Task "查看页面并返回页面标题"
.\run.cmd -Url "https://你的测试网站" -Task "你的任务指令" -MaxSteps 20
```

默认启动新的浏览器会话，不自动继承个人 Chrome 登录状态。`auto` 依次尝试下载的 Chromium、已安装的 Edge、Chrome，并在发生切换时打印实际选择。可加 `-Headless` 后台执行，或 `-Channel chromium` / `-Channel chrome` / `-Channel msedge` 固定浏览器。运行轨迹和报告保存到 `artifacts/qwen-browser/`（每次覆盖同名文件）。若要自定义输出目录、接入已有 CDP 浏览器或调整更多参数，使用底层入口：

```powershell
.\.venv\Scripts\python.exe -m osworld_agent.run_browser_windows --help
```

底层 Python 入口本身不读取 `.env`；`run.cmd` 负责加载。可选 UI-TARS 视觉定位配置为 `.env` 中的 `GROUNDING_API_URL`、`GROUNDING_MODEL`、`GROUNDING_API_KEY`；未配置时浏览器任务使用 DOM/AX 动作，对需要纯视觉定位的内容不能保证执行。

## 4. 完整桌面模式：迁移 OSWorld + VMware，再一键启动 Web GUI

Git clone 只迁移本仓库代码。原机器的 OSWorld 源码、任务集和 VM 不在这个仓库里，需要通过其原仓库或文件传输单独准备，建议结构：

```text
osworld_agent/
  external/OSWorld/                # 与原机相同提交的外部仓库
    desktop_env/
    requirements.txt
    evaluation_examples/examples/
    vmware_vm_data/Ubuntu0/        # 整个 VM 目录，不只 .vmx
      Ubuntu0.vmx
      *.vmdk / *.vmsn / ...        # 磁盘及快照链
```

原机在包含 `desktop_env` 的外部仓库运行 `git remote -v`、`git rev-parse HEAD`，记录真实远端和提交；新机 clone 该远端到 `external/OSWorld` 并 checkout 同一提交。不要只凭仓库名称换成另一分支；外部仓库必须实际包含 `desktop_env/`。任务集若在另一目录，也单独复制并修改路径。

先正常关闭虚拟机，再复制整个 VM 目录和 `init_state` 快照到新机；在新机安装 VMware Workstation，确认 `vmrun.exe` 可用，并把其安装目录加入 PATH。已有 VM 的准备细节见 [本地环境搭建指南](docs/本地环境搭建指南.md)。安装完成后编辑 `.env`：

```dotenv
OSWORLD_DESKTOP_ENV_PATH=external/OSWorld
OSWORLD_EXAMPLES_DIR=external/OSWorld/evaluation_examples/examples
OSWORLD_VM_PATH=external/OSWorld/vmware_vm_data/Ubuntu0/Ubuntu0.vmx
OSWORLD_SNAPSHOT_NAME=init_state
OSWORLD_MODEL_PRESETS=config/model_presets.local.yaml
AGENTS_RESULTS_DIR=artifacts/viz
AGENTS_VIZ_PORT=8088
```

相对路径从本仓库根目录计算，也支持含空格的绝对路径。编辑 `config/model_presets.local.yaml`，配置新电脑能访问的决策模型及 grounding 模型的名称、URL、Key 和 grounding `type`。此文件从现有预设复制生成并被 Git 忽略；桌面 GUI 使用这个预设文件，浏览器 CLI 使用 `QWEN_*` 配置。

快照名以实际 VM 为准：便携脚本默认使用 `init_state`；若原机使用 `init_state_ca3` 或其他名称，把 `OSWORLD_SNAPSHOT_NAME` 改为该名称。检查和任务运行读取同一个配置。

最后：

```powershell
.\setup.cmd -Profile desktop
.\run.cmd -Mode viz -Profile desktop
```

桌面安装会把外部 OSWorld 的依赖装入本仓库 `.venv`，然后安装 Agent。若外部版本依赖不适用于 Windows或与本仓库冲突，安装会报错停止；需要使用该外部版本的 Windows 依赖清单，可通过 `OSWORLD_DESKTOP_REQUIREMENTS` 指定。脚本不会自动删依赖或替换外部版本。

启动前检查 `desktop_env` 导入、任务 JSON、`vmrun`、`.vmx`、`init_state` 快照和模型预设；不自动启动或回退 VM。通过后启动 GUI 并自动打开 `http://127.0.0.1:8088`，在网页选择任务/模型并开始运行，Ctrl+C 停止服务器。需要任务环境自身的上游代理时才设置 `OSWORLD_UPSTREAM_PROXY`；已移除强制使用原电脑内网代理的默认值。

**若没有迁移 VM/任务集或无法访问远程模型，只有 clone 代码不能完成桌面任务。** VMware 安装、VM 文件传输和远程模型部署不属于浏览器一键安装脚本自动完成的部分。

## 5. 排查与更新

```powershell
.\run.cmd -Mode doctor                         # Python/浏览器自检，不要求模型 Key
.\run.cmd -Mode doctor -CheckApi               # 额外检查配置端点的 GET /models
.\run.cmd -Mode doctor -Profile desktop        # 额外检查 OSWorld/VMware/快照
git pull
.\setup.cmd                                   # 更新包与对应 Chromium；本地 .env 不覆盖
```

`-CheckApi` 不发起推理请求；部分服务没有 `/models` 接口，此检查失败不一定意味着推理不可用，也不会验证视觉能力。安装下载失败时检查网络、代理或企业证书；可通过现有 `PIP_INDEX_URL`、`HTTPS_PROXY`、`NODE_EXTRA_CA_CERTS`、`PLAYWRIGHT_DOWNLOAD_HOST` 设置合法的源/证书。若 Python 已卸载导致旧 `.venv` 失效，把 `.venv` 重命名为 `.venv.old` 后重装。

一键脚本使用 `requirements-windows.lock.txt` 固定已验证的浏览器/Web GUI 运行依赖版本，并安装与该 Playwright 版本配套的 Chromium。更新依赖时应主动更新此清单并重新跑自检。Python 3.12 的补丁版本、系统 Edge/Chrome 版本和外部 OSWorld 依赖不由这份清单固定；完整桌面复现还需要记录外部仓库提交及其依赖版本。不要从原机包含系统包的 venv 直接导出整份依赖。

安装方式参考 [WinGet install 官方文档](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)、[Playwright 浏览器安装文档](https://playwright.dev/python/docs/browsers) 和 [setuptools 包路径配置](https://setuptools.pypa.io/en/stable/userguide/package_discovery.html)。
