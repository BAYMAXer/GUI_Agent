# 版本记录

Git 标签固定每一版代码，`main` 提供最新交付版。标签与 Python 包版本用途不同：第一版源码中的包元数据为 0.1.0，但这次按用户要求标记为第一版 `v1.0.0`。

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
git clone --branch v1.0.0 https://github.com/BAYMAXer/master.git osworld_agent_v1
git clone --branch v2.0.0 https://github.com/BAYMAXer/master.git osworld_agent_v2
```

无需覆盖当前工作目录。已有仓库切换标签前应保留未提交修改；需要修改标签版本时先创建工作分支。
