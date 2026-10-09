# 版本记录

Git 标签固定每一版代码，`main` 提供最新交付版。标签与 Python 包版本用途不同：第一版源码中的包元数据为 0.1.0，但这次按用户要求标记为第一版 `v1.0.0`。

## 第三版增量：v3.1.0

- `COMPUTER_MONITOR` / `-Monitor` 固定主屏或 doctor 枚举的显示器设备，doctor、smoke、acceptance 与普通 computer 任务共用选屏配置。
- 模型只接收工作屏截图；物理坐标转换覆盖负坐标副屏和不同缩放，点击、滚动与拖拽限定工作屏。已有任务窗口须完整移入该屏，新开的浏览器及测试窗口自动放入工作区。
- 每次观察、动作执行和实际输入前检查任务前台、焦点事件及布局。抢占后最多恢复一次最近有效任务窗口并重新观察，恢复失败、重复抢占或工作屏条件改变时保存安全停止报告并返回非零。
- Agent 的 Alt+Tab 仅切换本任务已合法确认且仍位于工作屏的窗口，不使用系统全局窗口顺序；安全停止后恢复条件并重新启动，不支持断点续跑。
- 更新新电脑自动执行文档与最新固定版本 clone 命令；保留 `v1.0.0`、`v2.0.0`、`v3.0.0` 历史标签。

## 第三版：v3.0.0

- Windows 主机 `computer` profile 运行通用 GUI Agent，一个任务连续跨记事本、网页和普通应用，共用状态、记忆及统一 v3 轨迹。
- 浏览器为按需启动的可选增强；可配置 Chrome/Edge exe、专用 profile、下载目录和已有本机 CDP。仅匹配的前台网页焦点加入精简 AX；地址栏、原生对话框、其他应用或结构失败时恢复视觉操作。
- uv 安装与 doctor 适配 computer；不依赖 OSWorld 或 VMware，默认不下载可选浏览器、不注入输入。
- 增加 computer 工程 smoke 与真实模型分阶段跨应用验收；独立核对下载论文和记事本保存结果，并区分 `blocked`、`failed`、`passed`。
- computer 可配置独立定位服务，也可复用支持所选定位协议的 PLAN 服务；不把工程替身策略当作真实模型或训练数据。
- 新机自动执行文档以显式 `-Profile computer` 为主线。脚本默认 profile 仍为 `browser`；浏览器专项和 VMware 保留各自入口及验收边界。

第一版、第二版和第三版标签保持原代码，不覆盖。最新增量固定版本命令适用于发布后的 `v3.1.0` 标签；迁移最新主线也可 clone `main` 并记录具体提交。

## 第二版：v2.0.0

- Windows 便携安装改用 uv，固定安装器版本、默认 Python 补丁版本及带哈希的依赖锁。
- CMD/PowerShell 启动器支持中文和空格路径、首次安装、环境更新及可保留旧环境的重建。
- 决策与定位服务通过 `.env` 独立配置，支持通用模型 ID 与定位协议；提供浏览器及 VMware CLI。
- 增加分阶段验收报告，分别验证运行环境、固定动作策略和配置的真实模型；任务失败返回非零。
- 收录当前浏览器定位、模型配置、轨迹与指标的代码改动。
- 提供 `WINDOWS_AGENT_RUNBOOK.md`，可交由另一台电脑的 Agent 读取并执行。

## 第一版：v1.0.0

保存本次改动前已经上传的提交 `03274cc6723dc237458f316336efbefc34432948`。包含原 Agent 框架、浏览器/VMware 适配器、pip 安装脚本与原始 Windows 启动方案。

```bat
git clone --branch v1.0.0 https://github.com/BAYMAXer/GUI_Agent.git osworld_agent_v1
git clone --branch v2.0.0 https://github.com/BAYMAXer/GUI_Agent.git osworld_agent_v2
git clone --branch v3.0.0 https://github.com/BAYMAXer/GUI_Agent.git osworld_agent_v3
git clone --branch v3.1.0 https://github.com/BAYMAXer/GUI_Agent.git osworld_agent_v3_1
```

无需覆盖当前工作目录。已有仓库切换标签前应保留未提交修改；需要修改标签版本时先创建工作分支。
