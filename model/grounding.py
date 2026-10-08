"""视觉定位模型（grounding）层：把「点搜索框」这种自然语言目标定位成坐标。

决策与定位使用独立模型；网页节点与普通 GUI 坐标共用请求和结果结构。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple
import base64
from io import BytesIO
import math
import copy
import time

from ..config import GroundingConfig
from ..config.model_registry import GROUNDING_REGISTRY, resolve_model_name, resolve_grounding_protocol, environment_setting


def screenshot_image(screenshot):
    from PIL import Image
    if isinstance(screenshot, Image.Image):
        return screenshot
    if isinstance(screenshot, bytes):
        return Image.open(BytesIO(screenshot)).convert("RGB")
    if isinstance(screenshot, str) and screenshot.startswith("data:"):
        return Image.open(BytesIO(base64.b64decode(screenshot.split(",", 1)[1]))).convert("RGB")
    if isinstance(screenshot, str):
        return Image.open(screenshot).convert("RGB")
    raise ValueError("A real screenshot is required for grounding")


def capture_legacy_call(model, messages, generation, started, *, response=None, error=None, metadata=None):
    """Record the actual legacy protocol without inventing sampled token IDs."""
    call = {"actor_role": "grounding", "provenance": "sampled", "model": model,
            "messages": copy.deepcopy(messages), "generation": copy.deepcopy(generation),
            "policy_revision": None, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "prompt_metadata": metadata or {}, "valid_action": False}
    if response is not None:
        choice = response.choices[0]
        call.update(response=choice.message.content or "", finish_reason=choice.finish_reason,
                    response_model=response.model, response_id=response.id,
                    usage=response.usage.model_dump() if response.usage else None)
    if error is not None:
        call["error_type"] = type(error).__name__
    return call


@dataclass
class GroundingRequest:
    screenshot: Any
    target: str
    action: str = "click"
    mode: str = "point"  # node / point; same envelope for desktop and browser
    target_hint: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)
    snapshot_id: str = ""
    ref_aliases: dict = field(default_factory=dict)
    subgoal: str = ""
    expanded: bool = False
    roi: Optional[tuple] = None  # verified rectangle in the original screenshot's pixels
    feedback: str = ""


@dataclass
class GroundingResult:
    """一次定位结果：中心坐标 + 置信度（可选）。"""
    x: int = -1
    y: int = -1
    confidence: float = 1.0
    status: str = "ok"
    target_ref: Optional[str] = None
    point: Optional[tuple] = None  # normalized 0..1000 in the actual model input image
    reason: str = ""
    metadata: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def __post_init__(self):
        if self.status == "ok" and not self.target_ref and (self.x < 0 or self.y < 0 or self.confidence <= 0):
            self.status = "not_found"


class Grounding(ABC):
    """节点/视觉定位接口，旧 locate() 客户端仍可使用。"""

    supports_nodes = False

    def resolve(self, request: GroundingRequest) -> GroundingResult:
        if request.mode == "node":
            return GroundingResult(status="not_found", reason="This grounding protocol has no node-selection capability")
        result = self.locate(request.screenshot, request.target)
        result.calls = list(getattr(self, "last_calls", []))
        return result

    @abstractmethod
    def locate(self, screenshot, target: str) -> GroundingResult:
        """给定截图 + 目标描述，返回要点击的中心坐标。"""
        raise NotImplementedError


class PixelGrounding(Grounding):
    """像素点定位客户端。

    输入截图和目标描述，使用配置的坐标分辨率换算实际点击位置。
    """

    def __init__(self, url: str, api_key: str, model: str,
                 width: int = 1920, height: int = 1080, timeout: float = 180.0):
        self.url = url
        self.api_key = api_key
        self.model = model
        self.width = width
        self.height = height
        self.timeout = timeout

    def locate(self, screenshot, target: str) -> GroundingResult:
        """定位目标元素。

        复用 adapters.pixel_grounding 的真实协议，持久化客户端连接。
        """
        from ..adapters.pixel_grounding import PixelGroundingClient
        if not hasattr(self, "_delegate"):
            self._delegate = PixelGroundingClient(self.url, self.api_key, self.model,
                self.width, self.height, timeout=self.timeout)
        result = self._delegate.locate(screenshot, target)
        self.last_calls = self._delegate.last_calls
        return result

    def close(self):
        if hasattr(self, "_delegate"):
            self._delegate.close()


class NullGrounding(Grounding):
    """没有定位服务时显式失败，绝不制造屏幕中心坐标。"""

    def __init__(self, width: int = 1920, height: int = 1080):
        self.width = width
        self.height = height

    def locate(self, screenshot, target: str) -> GroundingResult:
        return GroundingResult(x=-1, y=-1, confidence=0.0)


def resolve_coords(gr: GroundingResult, screenshot=None) -> Optional[Tuple[int, int]]:
    """把定位结果转成坐标；grounding 失败（置信度 0 或负坐标）时返回 None。

    注意：不要在这里把失败结果钳制成 (0,0)——那会让执行器"以为"定位成功而去点
    左上角。失败必须显式返回 None，由执行器拒绝执行。
    """
    if gr is None:
        return None
    if gr.status != "ok" or gr.target_ref:
        return None
    if gr.confidence is not None and gr.confidence <= 0:
        return None
    if (isinstance(gr.x, bool) or isinstance(gr.y, bool) or
            not isinstance(gr.x, (int, float)) or not isinstance(gr.y, (int, float)) or
            not math.isfinite(gr.x) or not math.isfinite(gr.y) or gr.x < 0 or gr.y < 0):
        return None
    if screenshot is not None:
        try:
            w, h = screenshot_image(screenshot).size
            if gr.x >= w or gr.y >= h:
                return None
        except (ValueError, OSError):
            return None
    return int(gr.x), int(gr.y)


def mark_coordinate(screenshot, x: int, y: int, radius: int = 14, color: str = "red"):
    """在截图上画一个小圆圈，标记 grounding 定位坐标，返回新图。

    用于视觉记忆：Mark(S(t-1)) 标出上一步实际 grounding 点击位置。
    截图需是 PIL.Image（有 .convert/.copy）；否则原样返回。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return screenshot
    if not hasattr(screenshot, "convert"):
        return screenshot
    img = screenshot.convert("RGB").copy()
    draw = ImageDraw.Draw(img)
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=color, width=3)
    return img


def build_grounding_model(cfg: GroundingConfig) -> Grounding:
    """按配置的协议构造定位客户端，不根据模型名称写业务分支。"""
    protocol = resolve_grounding_protocol(cfg.name, cfg.protocol)
    cfg = replace(cfg, name=resolve_model_name(cfg.name), protocol=protocol)
    if protocol == "structured":
        from ..adapters.grounding import GroundingClient
        return GroundingClient(cfg)
    key = environment_setting(cfg.api_key_env) if cfg.api_key_env else cfg.api_key
    if protocol == "normalized":
        from ..adapters.normalized_grounding import NormalizedGroundingClient
        return NormalizedGroundingClient(cfg.url, key, cfg.name, cfg.width, cfg.height, timeout=cfg.timeout)
    return PixelGrounding(url=cfg.url, api_key=key, model=cfg.name,
                           width=cfg.width, height=cfg.height, timeout=cfg.timeout)
