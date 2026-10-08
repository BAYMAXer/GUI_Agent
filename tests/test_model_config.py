"""Deployment changes must not change planning or grounding code paths."""
import json

import httpx
import pytest
from openai import OpenAI
from PIL import Image

from osworld_agent.adapters.normalized_grounding import NormalizedGroundingClient
from osworld_agent.adapters.pixel_grounding import PixelGroundingClient
from osworld_agent.config import GroundingConfig, ModelConfig
from osworld_agent.config.model_registry import (
    GROUNDING_REGISTRY, MODEL_REGISTRY, PROTOCOL_ALIASES, ModelEntry,
    environment_aliases, environment_setting, resolve_grounding_protocol,
)
from osworld_agent.model.decision_model import ChatModel
from osworld_agent.model.grounding import GroundingRequest, build_grounding_model, resolve_coords


@pytest.mark.parametrize("protocol", ["structured", "pixel", "normalized"])
def test_arbitrary_deployment_id_uses_configured_protocol(monkeypatch, protocol):
    recorded = []
    name = "customer-grounding-deployment-v2"

    def handler(request):
        recorded.append(json.loads(request.content))
        output = ('{"status":"ok","target_ref":null,"point":[500,500]}'
                  if protocol == "structured" else "[200,100]" if protocol == "pixel" else "[500,500]")
        return httpx.Response(200, json={"id": "protocol", "object": "chat.completion", "created": 0,
            "model": name, "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": output}}]})

    client = OpenAI(api_key="test-only", base_url="http://protocol.test/v1",
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(PixelGroundingClient, "_get_client", lambda self: client)
    monkeypatch.setattr(NormalizedGroundingClient, "_get_client", lambda self: client)
    ground = build_grounding_model(GroundingConfig(name=name, protocol=protocol, width=400, height=200))
    if protocol == "structured":
        ground.model._client = client
    try:
        result = ground.resolve(GroundingRequest(Image.new("RGB", (400, 200)), "Save"))
        assert resolve_coords(result) == (200, 100)
        assert recorded[0]["model"] == name
        assert result.calls[0]["actor_role"] == "grounding"
        assert ground.supports_nodes == (protocol == "structured")
    finally:
        ground.close()
        client.close()


def test_role_alias_selects_registered_protocol_and_allows_override(monkeypatch):
    monkeypatch.setitem(GROUNDING_REGISTRY, "grounding", ModelEntry("tenant-endpoint-v3", "test", "normalized"))
    ground = build_grounding_model(GroundingConfig(name="grounding"))
    assert isinstance(ground, NormalizedGroundingClient) and ground.model == "tenant-endpoint-v3"
    override = build_grounding_model(GroundingConfig(name="grounding", protocol="structured"))
    assert override.supports_nodes and override.model.cfg.name == "tenant-endpoint-v3"
    for old_value, canonical in PROTOCOL_ALIASES.items():
        assert GroundingConfig(protocol=old_value).protocol == canonical
    assert resolve_grounding_protocol("unregistered-model") == "structured"
    with pytest.raises(ValueError, match="Unknown grounding protocol"):
        GroundingConfig(protocol="unknown-protocol")


def test_planning_alias_and_legacy_environment_credentials(monkeypatch):
    monkeypatch.setitem(MODEL_REGISTRY, "planning", ModelEntry("tenant-planning-v2", "test"))
    aliases = environment_aliases()["PLAN_API_KEY"]
    for name in ["PLAN_API_KEY", *aliases]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(aliases[0], "legacy-test-key")
    assert environment_setting("PLAN_API_KEY") == "legacy-test-key"
    recorded = []

    def handler(request):
        recorded.append((json.loads(request.content), request.headers["authorization"]))
        return httpx.Response(200, json={"id": "planning", "object": "chat.completion", "created": 0,
            "model": "tenant-planning-v2", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"}}]})

    # Exercise the lazy client creation with environment resolution and a local transport.
    class LocalClient(httpx.Client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", LocalClient)
    for expected_key in ["legacy-test-key", "canonical-test-key"]:
        if expected_key == "canonical-test-key":
            monkeypatch.setenv("PLAN_API_KEY", expected_key)
        model = ChatModel(ModelConfig(name="planning", url="http://protocol.test/v1", api_key_env="PLAN_API_KEY"))
        try:
            assert model.chat([{"role": "user", "content": "Ready?"}]) == "ok"
            body, authorization = recorded[-1]
            assert body["model"] == "tenant-planning-v2"
            assert authorization == "Bearer " + expected_key
        finally:
            model.close()
