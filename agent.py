"""智能体：组件持有者 + 对外入口。

真正的「观察 → 决策 → 定位 → 执行 → 记忆更新」主循环在 pipeline.py 的 Pipeline 里，
这里只负责：持有各组件（决策模型 / grounding / 环境）+ 构造 system prompt +
把 run 委托给 Pipeline。

想改整体流程（加阶段、改早停、改记忆）→ 去 pipeline.py 的 Pipeline.run()。
"""
from __future__ import annotations

from .env import Environment
from .model import ChatModel, Grounding
from .code_agent import CodeAgent
from .pipeline import Pipeline, AgentResult
from .prompts import build_system_prompt


class Agent:
    """GUI 操作智能体（对外入口）。"""

    def __init__(self, decision_model: ChatModel, grounding: Grounding,
                 env: Environment, max_steps: int = 20, max_retries: int = 5,
                 enable_reflection: bool = True, early_stop_repeat: int = 3,
                 code_agent_budget: int = 20, context_config=None):
        self.model = decision_model
        self.grounding = grounding
        self.env = env
        self.max_steps = max_steps
        self.max_retries = max_retries
        self.enable_reflection = enable_reflection
        self.early_stop_repeat = early_stop_repeat
        self.context_config = context_config
        self.os_name = getattr(env, "os_name", "linux")
        allowed = getattr(env, "supported_actions", None)
        from .actions import ACTION_REGISTRY
        capabilities = getattr(env, "capabilities", set())
        registry = {k: v for k, v in ACTION_REGISTRY.items()
                    if (allowed is None or k in allowed) and (not v.capability or v.capability in capabilities)}
        if allowed is not None:
            self.system_prompt = build_system_prompt(os_name=self.os_name,
                action_registry=registry, skill_registry={})
        else:
            self.system_prompt = build_system_prompt(os_name=self.os_name, action_registry=registry)
        self.code_agent = CodeAgent(self.model, self.env, budget=code_agent_budget)

    def run(self, task: str) -> AgentResult:
        """执行一个任务，返回结果。主循环在 pipeline.Pipeline 里。"""
        pipeline = Pipeline(
            model=self.model,
            grounding=self.grounding,
            env=self.env,
            system_prompt=self.system_prompt,
            code_agent=self.code_agent,
            max_steps=self.max_steps,
            max_retries=self.max_retries,
            enable_reflection=self.enable_reflection,
            early_stop_repeat=self.early_stop_repeat,
            context_config=self.context_config,
        )
        return pipeline.run(task)
