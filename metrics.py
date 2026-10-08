"""Separate retrieval, model accuracy and execution. Unknown labels stay unknown."""
from __future__ import annotations

import math
import statistics


def latency_summary(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and math.isfinite(v))
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None}
    return {"count": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(values[max(0, math.ceil(.95 * len(values)) - 1)], 2)}


def summarize_trajectory(trajectory):
    calls = [c for s in trajectory.get("steps", []) for c in s.get("model_calls", [])]
    ground = [c for c in calls if c.get("actor_role") == "grounding"]
    steps = trajectory.get("steps", [])
    point_actions = {"click", "double_click", "right_click", "type", "select", "drag_and_drop"}
    targeted = [s for s in steps if (s.get("resolved_action") or s.get("action") or {}).get("action") in point_actions]
    retrieval_hits, view_hits, selected_hits, point_hits = [], [], [], []
    for s in steps:
        meta = s.get("grounding_metadata", {})
        labels = s.get("grounding_labels", {})
        expected = labels.get("target_ref")
        if expected is not None:
            retrieval_hits.append(expected in [n["ref"] for n in meta.get("candidate_view", [])])
            sampled = [c for c in s.get("model_calls", []) if c.get("actor_role") == "grounding"]
            node_calls = [c for c in sampled if c.get("prompt_metadata", {}).get("mode") == "node"]
            if node_calls:
                visible = expected in node_calls[-1].get("prompt_metadata", {}).get("candidate_refs", [])
                view_hits.append(visible)
                if visible:
                    selected_hits.append(node_calls[-1].get("resolved_target", {}).get("target_ref") == expected)
        bbox = labels.get("bbox")
        coord = (s.get("grounding") or {}).get("coord")
        if bbox is not None and coord is not None and meta.get("mode") == "point":
            x, y, w, h = bbox
            point_hits.append(x <= coord[0] < x + w and y <= coord[1] < y + h)
    actual = [c.get("usage", {}).get("prompt_tokens") for c in calls if c.get("usage")]
    estimated = [c.get("prompt_metadata", {}).get("input_estimate") for c in calls]
    outcome = trajectory.get("outcome", {})
    return {
        "task_success": outcome.get("score", 0) >= 1 if outcome.get("evaluation_available") else None,
        "candidate_recall": statistics.mean(retrieval_hits) if retrieval_hits else None,
        "candidate_label_count": len(retrieval_hits),
        "candidate_view_recall": statistics.mean(view_hits) if view_hits else None,
        "candidate_view_label_count": len(view_hits),
        "node_selection_accuracy": statistics.mean(selected_hits) if selected_hits else None,
        "node_label_count": len(selected_hits),
        "visual_hit_rate": statistics.mean(point_hits) if point_hits else None,
        "visual_label_count": len(point_hits),
        "dom_coverage": (sum(s.get("routing", {}).get("channel") == "browser_dom" for s in targeted)
                         / len(targeted)) if targeted else None,
        "grounding_calls": len(ground),
        "grounding_latency": latency_summary([c.get("latency_ms") for c in ground]),
        "step_latency": latency_summary([s.get("timing_ms", {}).get("total") for s in steps]),
        "capture_latency": latency_summary([(s.get("observation", {}).get("context") or [{}])[0].get("capture_ms") for s in steps]),
        "input_tokens_actual": {"count": len(actual), "sum": sum(actual), "max": max(actual, default=None)},
        "input_tokens_estimated": {"count": len([v for v in estimated if v is not None]),
            "max": max((v for v in estimated if v is not None), default=None)},
    }
