"""决策模型层：可插拔注册表 + OpenAI 兼容客户端（支持视觉）。

换决策模型只改两处之一：
1. 改 config.yaml 里的 model.name（对应 MODEL_REGISTRY 的键）；
2. 或在 MODEL_REGISTRY 里新增一项。

所有模型都走 OpenAI 兼容 /chat/completions，支持本地或远程的兼容服务。
vision 开启时（默认），每步把当前截图以 OpenAI vision 格式（image_url）塞进用户消息。
"""
from __future__ import annotations

import time
import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..config import ModelConfig
from ..config.model_registry import MODEL_REGISTRY, resolve_model_name, environment_setting


@dataclass
class ChatMessage:
    role: str  # system / user / assistant
    content: str


def _guess_mime(path: str) -> str:
    import mimetypes
    return mimetypes.guess_type(path)[0] or "image/png"


def encode_image(screenshot) -> str:
    """把截图转成 base64 data URL（OpenAI vision 的 image_url 用）。

    支持 PIL.Image / 文件路径 / bytes / 已是 data-url 的字符串。
    无法识别时返回空字符串，调用方会回退到纯文本。
    """
    import base64
    from io import BytesIO

    if screenshot is None:
        return ""
    if isinstance(screenshot, str) and screenshot.startswith("data:"):
        return screenshot
    if isinstance(screenshot, bytes):
        return "data:image/png;base64," + base64.b64encode(screenshot).decode()
    if isinstance(screenshot, str):
        try:
            with open(screenshot, "rb") as f:
                data = f.read()
        except OSError:
            return ""
        return f"data:{_guess_mime(screenshot)};base64," + base64.b64encode(data).decode()
    # PIL.Image 或任何有 save 的对象
    try:
        buf = BytesIO()
        img = screenshot.convert("RGB") if hasattr(screenshot, "convert") else screenshot
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


class ChatModel:
    """OpenAI 兼容的决策模型客户端（带超时与重试，支持视觉）。"""

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.vision = bool(getattr(cfg, "vision", True))
        self.disable_thinking = bool(getattr(cfg, "disable_thinking", True))
        self._client = None
        self.last_call = {}

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def chat(self, messages, retries: int = 2, json_mode: bool = False) -> str:
        """发送一轮对话，返回模型文本；失败自动重试。

        messages 既可以是 ChatMessage 列表，也可以是 {"role","content"} dict 列表，
        content 可以是字符串（纯文本）或 list（OpenAI vision 多部分），内部统一规范化。

        json_mode=True 时加 response_format=json_object，用 vLLM 约束解码强制输出合法
        JSON 对象（减少非 JSON、格式漂移）；这是「约束前移」，仍保留调用方的执行前校验。
        """
        from openai import OpenAI  # 延迟导入，避免非模型场景也依赖 openai
        import httpx

        normalized = [self._to_message_dict(m) for m in messages]
        self.last_call = {"messages": copy.deepcopy(normalized), "model": resolve_model_name(self.cfg.name),
                          "policy_revision": self.cfg.policy_revision or None, "attempts": []}

        # trust_env=False：直连内网 endpoint，不走系统代理（同 grounding，避免 HIS 代理拦掉 7.x）
        if self._client is None:
            key = environment_setting(self.cfg.api_key_env) if self.cfg.api_key_env else self.cfg.api_key
            if not key:
                raise ValueError(f"Missing API key environment variable: {self.cfg.api_key_env}")
            self._client = OpenAI(
                base_url=self.cfg.url, api_key=key, max_retries=0,
                http_client=httpx.Client(trust_env=self.cfg.trust_env, timeout=self.cfg.timeout),
            )
        client = self._client
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                create_kwargs: Dict[str, Any] = dict(
                    model=resolve_model_name(self.cfg.name),
                    messages=normalized,
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_tokens,
                )
                # 按服务协议关闭思考模式，限制生成长度并尽快得到决策结果。
                if self.disable_thinking and self.cfg.thinking_style == "vllm":
                    create_kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
                elif self.disable_thinking and self.cfg.thinking_style == "dashscope":
                    create_kwargs["extra_body"] = {"enable_thinking": False}
                if json_mode:
                    create_kwargs["response_format"] = {"type": "json_object"}
                if self.cfg.collect_logprobs:
                    create_kwargs["logprobs"] = True
                if self.cfg.return_token_ids:
                    create_kwargs.setdefault("extra_body", {}).update(
                        return_token_ids=True, return_tokens_as_token_ids=True)
                generation = {k: v for k, v in create_kwargs.items() if k not in ("model", "messages")}
                self.last_call["generation"] = copy.deepcopy(generation)
                resp = client.chat.completions.create(**create_kwargs)
                msg = resp.choices[0].message
                content = getattr(msg, "content", "") or ""
                lp = getattr(resp.choices[0], "logprobs", None)
                lp_data = lp.model_dump() if lp else None
                lp_content = (lp_data or {}).get("content") or []
                ids = getattr(resp.choices[0], "token_ids", None)
                token_lps = [p.get("logprob") for p in lp_content]
                # Never retokenize an API response and pretend those are server-sampled IDs.
                if ids is None and lp_content and all(re.fullmatch(r"token_id:\d+", p.get("token", "")) for p in lp_content):
                    ids = [int(p["token"].split(":", 1)[1]) for p in lp_content]
                if not ids or len(ids) != len(token_lps):
                    ids, token_lps = None, None
                self.last_call.update({"response_id": resp.id, "response_model": resp.model,
                    "response": content, "generation": generation,
                    "finish_reason": resp.choices[0].finish_reason,
                    "usage": resp.usage.model_dump() if resp.usage else None,
                    "logprobs": lp_data, "token_ids": ids, "token_logprobs": token_lps,
                    "prompt_token_ids": getattr(resp, "prompt_token_ids", None),
                    "latency_ms": round((time.perf_counter()-started)*1000, 2)})
                self.last_call["attempts"].append({"status": "ok", "latency_ms": self.last_call["latency_ms"],
                    "generation": copy.deepcopy(generation), "response": content})
                return content
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self.last_call["attempts"].append({"status": "error", "error_type": type(exc).__name__,
                                                 "generation": copy.deepcopy(self.last_call.get("generation", {})),
                                                 "latency_ms": round((time.perf_counter()-started)*1000, 2)})
                # 若因 extra_body 不被支持而报错，去掉 extra_body 重试一次
                if self.disable_thinking and "extra_body" in str(exc).lower():
                    self.disable_thinking = False
                # 若 json_mode 不被支持，降级为无约束重试
                if json_mode and ("response_format" in str(exc).lower() or "json" in str(exc).lower()):
                    json_mode = False
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"模型调用失败({self.cfg.name}): {last_err}")

    def build_vision_message(self, text: str, images=None) -> Dict[str, Any]:
        """构造一条用户消息；vision 开启时按顺序附上多张图（OpenAI vision 格式）。

        images: [(label, screenshot), ...]，label 会作为图片前的文字说明（如 "S(t-1)"）。
        """
        if self.vision and images:
            content: Any = [{"type": "text", "text": text}]
            for label, shot in images:
                url = encode_image(shot)
                if not url:
                    continue
                if label:
                    content.append({"type": "text", "text": label})
                content.append({"type": "image_url", "image_url": {"url": url}})
            if len(content) > 1:
                return {"role": "user", "content": content}
        return {"role": "user", "content": text}

    @staticmethod
    def _to_message_dict(m) -> Dict[str, Any]:
        """把 ChatMessage 或 dict 统一成 {"role","content"}（content 可为 str 或 list）。"""
        if isinstance(m, dict):
            return {"role": m.get("role", "user"), "content": m.get("content", "")}
        return {"role": getattr(m, "role", "user"), "content": getattr(m, "content", "")}


def build_decision_model(cfg: ModelConfig) -> ChatModel:
    """根据配置构造决策模型客户端。"""
    return ChatModel(cfg)
