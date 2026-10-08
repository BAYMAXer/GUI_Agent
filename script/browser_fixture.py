"""Deterministic fixture policy; verifies plumbing without calling a model API."""
import copy
import json

from ..config import ModelConfig
from ..model.decision_model import ChatModel
from ..model.grounding import NullGrounding


def evaluate_customer(page):
    return page.evaluate("""() => {
        const r=JSON.parse(localStorage.getItem('customer-record')||'null');
        return !!r && r.customer==='Acme Ltd' && r.plan==='Enterprise' && r.notify===true && window.saveCount===1;
    }""")


class ScriptedPolicy(ChatModel):
    """Protocol/integration test only. This is NOT a model benchmark."""
    def __init__(self):
        super().__init__(ModelConfig(vision=True))
        self.index = 0
        self.inputs = []

    def chat(self, messages, **kwargs):
        self.inputs.append(copy.deepcopy(messages))
        content = messages[-1]["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        if "任务完成度核对器" in text:
            return "CONFIRM" if "Saved Acme Ltd" in text else "REJECT missing evidence"
        nodes = [json.loads(line) for line in text.splitlines() if line.startswith('{"ref":')]
        spec = [("type", "Customer name", {"text": "Acme Ltd", "overwrite": True}),
                ("select", "Plan", {"option": "Enterprise"}),
                ("click", "Enable notifications", {}),
                ("click", "Save customer", {}),
                ("request_finish", "", {"answer": "Customer saved"})][self.index]
        self.index += 1
        name, target, extra = spec
        action = {"type": name, **extra}
        if target:
            matches = [n for n in nodes if n.get("name") == target and n.get("direct")]
            if len(matches) != 1:
                raise RuntimeError(f"Fixture node is missing or ambiguous: {target}")
            action.update(target=target, target_ref=matches[0]["ref"])
        return json.dumps({"screen_analysis": "Settings form", "observed_effect": "Observed current state",
            "action_status": "success", "state_update": {}, "next_action": action,
            "expected_effect": "Saved state or updated form"})


class CountingGrounding(NullGrounding):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def locate(self, screenshot, target):
        self.calls += 1
        return super().locate(screenshot, target)
