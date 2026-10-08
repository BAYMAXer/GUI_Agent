"""适配层：把抽象框架接到真实的 OSWorld 环境与定位服务。

不改动框架任何现有文件，只在这里提供：
- VMwareEnvironment：实现 Environment 接口，包装 OSWorld 的 DesktopEnv；
- GroundingClient：结构化节点与视觉坐标双模式定位；
- PixelGroundingClient：像素坐标定位协议；
- NormalizedGroundingClient：[0,1000] 归一化坐标定位协议；
- task_loader：从 evaluation_examples 加载 task_config；
- run_vm：入口，组装环境 + grounding + agent 跑真实任务。
"""
from __future__ import annotations

from .vmware_env import VMwareEnvironment, to_pyautogui
from .grounding import GroundingClient
from .pixel_grounding import PixelGroundingClient
from .normalized_grounding import NormalizedGroundingClient
from .task_loader import load_task_config

__all__ = [
    "VMwareEnvironment", "to_pyautogui",
    "GroundingClient", "PixelGroundingClient", "NormalizedGroundingClient", "load_task_config",
]
