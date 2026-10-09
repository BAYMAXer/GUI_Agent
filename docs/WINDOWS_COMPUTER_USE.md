# Windows 通用 computer use

一个任务只创建一个 Agent，共用任务状态、业务记忆与轨迹。浏览器是按焦点启用的可选增强能力。普通应用只使用桌面截图，不读取桌面 AX 或记事本文件替代视觉感知。网页获焦点且本地结构接口可用时，决策输入包含截图和预算内语义；唯一匹配的网页目标直接验证执行，歧义交给 grounding，结构不足时回到视觉定位。

## 安装与初步验证

在已解锁、已登录的交互式 Windows x64 桌面执行；Agent 与目标应用权限一致。无需 OSWorld、VMware 或本机模型。脚本默认 profile 仍为 `browser`，以下显式使用 `computer`：

```powershell
.\setup.cmd -Profile computer -SkipSmoke
.\run.cmd -Mode doctor -Profile computer
.\run.cmd -Mode computer-smoke -Profile computer -Channel chrome -Output "artifacts/computer-smoke-chrome"
.\run.cmd -Mode computer-smoke -Profile computer -Channel msedge -Output "artifacts/computer-smoke-edge"
```

uv 安装器按 `.uv-version`、`.python-version` 和 `uv.lock` 建立新机器的隔离环境，不复制旧 `.venv`。computer 安装默认不下载浏览器；仅明确选择 Chromium 且未指定 exe/CDP 时安装对应浏览器。doctor 不启动浏览器，只检查截图和焦点，不注入输入；缺少可选浏览器不会阻断纯视觉任务。

computer-smoke 会操作真实窗口：显示独立测试文档，访问本机论文测试页，验证地址栏、文件对话框、不同浏览器实例、AX 失败、旧引用和焦点切换，然后通过同一 Agent 下载测试 PDF、回写中文并保存。它只关闭自身测试文档及自身浏览器，不操作其他记事本标签。测试策略及定位坐标是测试替身，报告 `model_verified=false`，轨迹标记 `use_for_model_training=false`；不能作为模型准确率、速度或训练数据。Chrome/Edge 路径可经 `.env` 的 `COMPUTER_BROWSER_EXECUTABLE` 或 smoke 的 `-BrowserExecutable` 指定。

报告位于指定目录的 `report.json`，截图与统一 v3 轨迹同目录。每次使用新的 fixture 子目录和下载目录，避免旧成功文件通过新验收。验收要求退出码 0、所有 checks 通过、独立文件核对 score=1。物理点击坐标来自实际桌面截图；负坐标显示器原点、显示器间隙、DPI 和布局变更会在执行层处理。

## 工作屏与前台保护

`COMPUTER_MONITOR=primary` 默认固定主屏；也可填 doctor 列出的设备名。任务启动时锁定该设备及显示布局，任务中不自动改屏。doctor 仅枚举、探测和截图，报告 `monitors` 中的 `device`、`primary`、物理矩形、`work_area`，以及 `target_monitor`、`capture_geometry`；不移动窗口，不恢复前台。设备配置无效时检查失败。

```powershell
.\run.cmd -Mode doctor -Profile computer
.\run.cmd -Mode doctor -Profile computer -Monitor "\\.\DISPLAY2"
.\run.cmd -Profile computer -Monitor "\\.\DISPLAY2" -Task "完成当前记事本任务"
```

同一选屏值会传给 setup doctor、computer-smoke 和 computer-acceptance。底层 Python 入口、doctor 和 smoke 使用 `--monitor`，未传时读 `COMPUTER_MONITOR`。已有窗口由用户完整移到工作屏并置于前台；跨屏窗口停止操作并提示调整。Agent 新开的浏览器及测试记事本自动放入工作屏工作区；显式接入的已有浏览器不移动。

模型只看到工作屏截图；`capture_geometry` 保存截图物理原点与尺寸，`desktop_geometry` 保存完整虚拟桌面。截图坐标加工作屏原点得到 Win32 物理坐标，SendInput 仍按完整虚拟桌面归一化，支持副屏负坐标及不同分辨率、缩放比例。点击、滚动位置和拖拽全路径须在工作屏内；网页结构操作也须验证整个浏览器窗口在工作屏内。

每次观察、动作执行前和实际发送输入前检查任务窗口、焦点事件版本与显示布局。仅启动确认、Agent 点击/明确切换窗口、打开任务浏览器及其所属对话框更新最近有效任务窗口。外部抢前台后废弃旧动作与定位，最多自动恢复一次最近任务窗口，恢复成功须重新截图、重新决策。恢复使用不注入键盘的 Win32 激活路径，并等待前台事件版本同步；系统拒绝激活、窗口关闭、已执行自动恢复之后又被抢前台（即使已切回原窗口）、窗口跨屏、目标屏消失或布局变化时安全停止。首次自动恢复之前短暂离开又返回时，旧观察和动作失效并重新观察，不按短暂切换次数直接停止。

Agent 显式输出 `hotkey(keys=["alt", "tab"])` 时，执行层从本任务已合法确认的窗口历史中选择最近仍有效的其他任务窗口，并用 Win32 激活；目标须仍位于工作屏内，跳过已关闭窗口、跨屏窗口和原生对话框。此动作不发送全局 Alt+Tab，因此不会根据 Windows 全局窗口顺序切入副屏或个人应用。没有可切换的任务窗口时拒绝动作，可通过工作屏任务栏点击选择应用。窗口历史仅用于这种显式切换；历史任务窗口自行抢前台仍按外部干扰处理，不能自动获得操作授权。

Win+R、Win+E 等普通快捷键创建的无所属关系新应用窗口不会自动获得任务授权，可能触发前台保护并恢复原任务窗口。启动或选择新应用时使用工作屏任务栏点击；快捷键的 `executed` 只表示已发送输入，须通过后续截图核对应用是否实际成为任务窗口。

安全停止后停止调用模型，产物目录仍保存轨迹和报告；报告 `safety_stop=true`、`termination_reason="safety_stop"`，`safety_stop_detail` 保存原因码与说明，`final_answer` 显示具体原因，退出码非零。人工恢复后重新启动任务；没有断点续跑。fixture 停止后的清理不会向前台发送保存或关闭组合键；需要时测试文档保留供检查。

副屏可继续显示内容，运行期间不要并行使用鼠标键盘。检查与输入之间仍有系统竞争；需要持续在副屏工作时使用独立虚拟机或独立交互会话。

## 真实任务与模型配置

按用户提供的 API 参数配置根目录 `.env`，不要把密钥写进命令行或轨迹：

```dotenv
PLAN_MODEL=实际决策服务模型ID
PLAN_API_URL=https://实际服务/v1
PLAN_API_KEY=实际密钥
PLAN_THINKING_STYLE=none
GROUNDING_MODEL=实际定位服务模型ID
GROUNDING_API_URL=https://实际定位服务/v1
GROUNDING_API_KEY=实际定位密钥
GROUNDING_PROTOCOL=structured
GROUNDING_THINKING_STYLE=none
```

`PLAN_*` 与 `GROUNDING_*` 分别配置模型职责，不改业务代码。computer 跨普通应用操作需要能从截图完成视觉定位的服务。当未配置独立 grounding URL 时，入口复用已配置的决策 API，定位协议仍由 `GROUNDING_PROTOCOL` / `-GroundType` 选择；`auto` 按注册表解析，通常 structured。复用的 API 必须同时支持规划与对应定位协议。报告记录该复用及实际模型角色；不能把代用模型采样当作另一个 checkpoint 的 on-policy 数据。独立定位服务可选。浏览器专项入口未配置 grounding 时仍保持原行为。

```powershell
.\run.cmd -Profile computer -Task "从当前记事本读取论文任务，打开学术检索网页，完成下载后回到记事本写入结果" -MaxSteps 30
.\run.cmd -Profile computer -Task "完成当前桌面任务" -Channel msedge -Output artifacts/my-computer-task
```

任务从当前桌面开始，不会自动打开浏览器或读取本地任务文件。模型可输出 `open_browser(url?)` 来创建或激活任务浏览器。同一任务中复用实例；`goto` 只在浏览器前台可用，结构不可用时通过地址栏输入导航。普通应用通过点击、拖拽、按键、组合键和 Unicode 文字操作。原 OSWorld/VMware 入口及浏览器专项入口仍可使用。

默认每次运行生成独立产物目录 `artifacts/computer-agent/<时间-标识>/`，包括报告、trajectory、decision SFT 和 grounding SFT。自定义任务没有独立 evaluator 时，分数为 null；模型声称完成不等于获得外部任务奖励。

computer 不支持 `-Headless`。锁屏、Windows 服务或断开的远程桌面会话不具备该运行条件；运行期间避免人工操作抢夺焦点。CMD 可直接使用 `run.cmd`，PowerShell 用 `.\run.cmd`。中文及空格路径参数需用引号包裹，例如 `-Output "D:\Agent 数据\本次任务"`。模型名称和 base URL 可由 `-Model` / `-ApiUrl` 覆盖，Key 始终放 `.env`。

## 新机器真实模型跨应用验收

配置实际模型后执行：

```bat
run.cmd -Mode computer-acceptance -Profile computer -Channel msedge -MaxSteps 30
```

该命令依次做 computer doctor、固定策略 computer-smoke、配置模型的本地跨应用论文任务。模型从测试记事本出发，检索 GUI Agent Browser Study、下载测试 PDF，再返回同一记事本文档，将全文替换为“已下载 GUI Agent Browser Study 论文 PDF。跨应用任务验证完成。”并保存。独立 evaluator 核对 `agent-study.pdf` 的字节、保存全文及当前前台文档；轨迹须包含桌面→网页→桌面。它使用随机隔离 fixture，不使用用户自己的文档或真实网站。

总报告为 `artifacts/computer-acceptance/report.json`，环境证据是 `doctor.json`，前置策略证据在 `smoke/`，真实模型证据在 `computer-agent/`。只有退出码 0、总报告 `status="passed"`、`success=true`、`real_model_verified=true`、`cross_application_verified=true`，且真实任务 `cross_application_verified=true`、`score=1.0`、`evaluation_available=true`、`reward_source="environment_evaluator"`，才算跑通。缺模型参数为 `blocked`，真实调用或任务失败为 `failed`，退出码非零。安装、scripted smoke 或模拟 HTTP 成功不能替代真实模型验收。

验收使用隔离 profile、CDP 与下载目录，不使用 `.env` 中持久登录配置；显式传 `-BrowserProfileDir` / `-CdpEndpoint` / `-DownloadDir` 会报错。自定义 exe/channel 仍可尊重新机设置。浏览器专项 `-Mode acceptance -Profile browser` 只测试网页表单，不能代替本阶段。完整自动执行步骤见 [新 Windows Agent 指南](WINDOWS_AGENT_RUNBOOK.md)。

## 浏览器配置与边界

| 配置 | 行为 |
| --- | --- |
| `COMPUTER_BROWSER_CHANNEL` | `auto` 优先已安装 Chrome，再 Edge，再已安装的 Playwright Chromium；可固定 `chrome` / `msedge` / `chromium` |
| `COMPUTER_MONITOR` | 默认 `primary`；可填 doctor 列出的显示器设备名，任务中固定工作屏 |
| `COMPUTER_BROWSER_EXECUTABLE` | 显式指定浏览器可执行文件，避免依赖安装位置 |
| `COMPUTER_CDP_ENDPOINT` | 接入已启动的本机 CDP 服务；不拥有、关闭或修改该浏览器的下载设置 |
| `COMPUTER_BROWSER_PROFILE_DIR` | 留空为任务独立目录；显式配置为 Agent 持久目录，保留 Agent 登录状态 |
| `COMPUTER_DOWNLOAD_DIR` | 自行启动浏览器的下载目录；留空保存到任务产物 downloads 目录 |

也可使用 `run.cmd` 的 `-BrowserExecutable`、`-CdpEndpoint`、`-BrowserProfileDir`、`-DownloadDir`、`-Monitor` 参数覆盖。独立 Agent 配置不继承个人浏览器 cookies 或扩展。已打开的持久目录应通过 CDP 显式接入，避免同时启动两次。

当前结构提供者采用 Chromium CDP，覆盖 Chrome/Edge。浏览器安装路径可发现或配置；企业策略关闭远程调试、未接入的其他实例以及其他浏览器仍可通过桌面视觉操作。不能据此声称 Firefox/Safari 已支持相同结构接口；需要新增相应适配器。

系统前台 HWND/PID、输入焦点、事件版本及真实网页焦点共同控制路由。适配器关闭自动化连接的焦点模拟，避免地址栏被误判为网页。CDP 的 browser PID 必须匹配当前系统前台进程；启动器委托另一进程时还核对该进程的配置目录。离开网页即失效结构引用；执行前再次核对焦点版本和桌面布局，推理期间切换后拒绝旧动作并重新观察。

DOM 坐标保持 CSS 空间，由浏览器执行。视觉坐标是截图的物理像素，叠加工作屏原点后由 Win32 执行，不混用两套坐标。截图不可用、定位越界或输入失败会记录拒绝/不确定，不伪造成功。

## 训练与后续评测

browser/computer 共用 Observation、GroundingRequest/Result、trajectory v3 和导出流程，网页步骤只增加可选 `observation.context`。记录实际模型输入、候选、图片变换、采样参数、重试、token 信息及执行结果。每次决策刷新后的观察与上一动作的 `next_observation` 对齐；可选 `post_action_observation` 另外保留执行后立即取得的画面，避免把两个不同画面赋予相同 ID。SFT 按模型角色导出；确定性 DOM 标签是离线监督，on-policy RL 必须使用对应 checkpoint 实际采样的 token/logprob。

真实模型平台分别测候选召回、节点选择、视觉命中、DOM 覆盖、任务成功、输入 tokens 与 P50/P95 时延。未配置 tokenizer/processor 的 token 数保持估算标记。初步工程验证不替代模型评测；真实 API 费用、效果与时延应在目标部署机器另外验收。
