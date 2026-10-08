"""Deployment identities and compatibility aliases, isolated from model logic."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path


@dataclass(frozen=True)
class ModelEntry:
    name: str
    description: str
    protocol: str = "structured"


MODEL_REGISTRY = {
    "planning": ModelEntry("Qwen3.5-9B", "默认决策服务"),
    "9b": ModelEntry("Qwen3.5-9B", "兼容已有决策配置"),
    "27b": ModelEntry("Qwen3.6-27B", "额外决策服务"),
}
GROUNDING_REGISTRY = {
    "grounding": ModelEntry("Qwen3.5-9B", "默认节点/视觉定位服务"),
    "grounding_pixel": ModelEntry("UI-TARS-1.5-7B", "像素点定位服务", "pixel"),
    "grounding_normalized": ModelEntry("UI-Venus-2-9B", "归一化点定位服务", "normalized"),
    "ui_tars_7b": ModelEntry("UI-TARS-1.5-7B", "兼容已有定位配置", "pixel"),
    "qwen3.5_9b": ModelEntry("Qwen3.5-9B", "兼容已有定位配置"),
    "UI-Venus-2": ModelEntry("UI-Venus-2", "兼容已有定位配置", "normalized"),
}
PROTOCOL_ALIASES = {"qwen": "structured", "ui-tars": "pixel", "ui-venus2": "normalized"}
GROUNDING_PROTOCOLS = ("auto", "structured", "pixel", "normalized")


def resolve_model_name(alias: str) -> str:
    entry = MODEL_REGISTRY.get(alias) or GROUNDING_REGISTRY.get(alias)
    return entry.name if entry else alias


def normalize_grounding_protocol(protocol: str) -> str:
    value = PROTOCOL_ALIASES.get(protocol, protocol)
    if value not in GROUNDING_PROTOCOLS:
        raise ValueError(f"Unknown grounding protocol: {protocol}")
    return value


def resolve_grounding_protocol(model: str, protocol="auto") -> str:
    value = normalize_grounding_protocol(protocol)
    if value != "auto":
        return value
    entry = GROUNDING_REGISTRY.get(model)
    if entry is None:
        entry = next((e for e in GROUNDING_REGISTRY.values() if e.name == model), None)
    return entry.protocol if entry else "structured"


@lru_cache(maxsize=1)
def environment_aliases():
    path = Path(__file__).with_name("environment_aliases.json")
    return json.loads(path.read_text(encoding="utf-8"))


def environment_setting(name, default=""):
    """Canonical role settings take precedence; old deployments remain readable."""
    aliases = environment_aliases().get(name, [])
    return next((os.environ[key] for key in [name, *aliases] if os.environ.get(key)), default)
