"""Local AX retrieval. Index size and model-view size are deliberately independent."""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache

CONTROLS = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
            "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
            "spinbutton", "listbox", "option", "treeitem"}
REGIONS = {"form", "row", "listitem", "dialog", "alertdialog", "region", "group",
           "article", "section", "main", "navigation", "table", "tabpanel", "grid",
           "LayoutTable", "LayoutTableRow"}
URGENT = {"alert", "alertdialog", "dialog", "status"}
STOP = {"the", "a", "an", "to", "of", "in", "on", "and", "or", "please", "click",
        "button", "target", "field", "input", "box"}


def normalized(text):
    return " ".join(str(text or "").casefold().split())


@lru_cache(maxsize=8192)
def terms(text):
    """Words plus Chinese bi/trigrams, rather than treating a sentence as one word."""
    result = set(re.findall(r"[a-z0-9_]+", normalized(text))) - STOP
    for chunk in re.findall(r"[\u3400-\u9fff]+", str(text)):
        if len(chunk) == 1:
            result.add(chunk)
        for size in (2, 3):
            result.update(chunk[i:i + size] for i in range(len(chunk) - size + 1))
    return frozenset(result)


def region_summary(ref, by_ref, children):
    """Bounded non-control facts, shared by retrieval and pre-execution validation."""
    labels, pending, visited, controls = [], list(children.get(ref, [])), set(), 0
    while pending and len(visited) < 64 and len(labels) < 8:
        child = pending.pop(0)
        if child in visited:
            continue
        visited.add(child)
        node = by_ref[child]
        controls += node.get("role") in CONTROLS
        text = node.get("name", "")
        owner = by_ref.get(node.get("parent"), {})
        duplicate = owner.get("role") in CONTROLS and text == owner.get("name")
        if text and text not in labels and node.get("role") not in CONTROLS and not duplicate:
            labels.append(text)
        if node.get("role") not in CONTROLS and node.get("role") not in REGIONS:
            pending.extend(children.get(child, []))
    return " | ".join(labels)[:400], controls, len(visited)


class BrowserIndex:
    def __init__(self, block):
        self.block = block
        self.nodes = block.get("nodes", [])
        self.by_ref = {n["ref"]: n for n in self.nodes}
        self.aliases = {f"n{i}": n["ref"] for i, n in enumerate(self.nodes)}
        self.short = {ref: alias for alias, ref in self.aliases.items()}
        self.children = {}
        for n in self.nodes:
            self.children.setdefault(n.get("parent"), []).append(n["ref"])
        self._contexts, self._paths, self._tokens = {}, {}, {}
        # Keep bounded text neighborhoods, including unlabeled div-based cards.
        self._region_text = {}
        self.region_refs = {n["ref"] for n in self.nodes if n.get("role") in REGIONS}
        for ref, children in self.children.items():
            parent = self.by_ref.get(ref, {})
            if parent.get("role") in REGIONS | {"generic"} or parent.get("structural_only"):
                text, controls, visits = region_summary(ref, self.by_ref, self.children)
                self._region_text[ref] = text
                if ((parent.get("role") == "generic" or parent.get("structural_only")) and text and 1 <= controls <= 4
                        and len(children) <= 16 and visits < 64):
                    self.region_refs.add(ref)
        df = Counter()
        for n in self.nodes:
            text = " ".join((n.get("name", ""), str(n.get("value", "")),
                             self.context(n["ref"])))
            self._tokens[n["ref"]] = terms(text)
            df.update(self._tokens[n["ref"]])
        self.idf = {t: math.log(1 + len(self.nodes) / (1 + count)) for t, count in df.items()}

    def canonical(self, ref):
        return self.aliases.get(ref, ref)

    def ancestors(self, ref):
        if ref not in self._paths:
            chain, seen = [], set()
            node = self.by_ref.get(ref)
            while node and node.get("parent") in self.by_ref and node["parent"] not in seen:
                seen.add(node["parent"])
                node = self.by_ref[node["parent"]]
                chain.append(node)
            self._paths[ref] = chain
        return self._paths[ref]

    def context(self, ref):
        if ref not in self._contexts:
            parts = []
            for node in reversed(self.ancestors(ref)):
                if node.get("role") == "RootWebArea":
                    continue
                if node["ref"] in self.region_refs or node.get("name"):
                    text = node.get("name", "") or self._region_text.get(node["ref"], "")
                    if text and text not in parts:
                        parts.append(text)
            self._contexts[ref] = " / ".join(parts[-4:])[:500]
        return self._contexts[ref]

    def scope(self, ref):
        return next((n["ref"] for n in self.ancestors(ref)
                     if n["ref"] in self.region_refs), None)

    def within(self, node, scope_ref):
        return not scope_ref or node["ref"] == scope_ref or any(
            n["ref"] == scope_ref for n in self.ancestors(node["ref"]))

    def score(self, node, query, hint=None):
        hint = hint or {}
        qterms = terms(query)
        name = normalized(node.get("name"))
        query_name = normalized(hint.get("name") or query)
        overlap = qterms & self._tokens[node["ref"]]
        relevance = sum(self.idf.get(t, 1) for t in overlap)
        score = relevance * 8
        if query_name and name == query_name:
            score += 100
        elif query_name and query_name in name:
            score += 25
        if hint.get("role") == node.get("role"):
            score += 12
        if hint.get("scope") and normalized(hint["scope"]) in normalized(self.context(node["ref"])):
            score += 40
        if node.get("states", {}).get("focused"):
            score += 8
        if node.get("in_viewport"):
            score += 4
        return score, bool(overlap or (query_name and query_name in name))

    def retrieve(self, target, *, action="click", hint=None, subgoal="", limit=20, scope_ref=None):
        hint = hint or {}
        scope_ref = self.canonical(scope_ref or hint.get("scope_ref"))
        if scope_ref and scope_ref not in self.by_ref:
            raise ValueError("Unknown scope reference")
        scored = []
        query = " ".join(filter(None, (target, hint.get("name", ""), hint.get("scope", ""), subgoal)))
        for n in self.nodes:
            if not n.get("direct") or n.get("states", {}).get("disabled") or not self.within(n, scope_ref):
                continue
            if action == "type" and (n.get("role") not in {"textbox", "searchbox", "combobox", "spinbutton"}
                                     or n.get("states", {}).get("readonly")):
                continue
            if action == "select" and n.get("role") != "combobox":
                continue
            if hint.get("role") and n.get("role") != hint["role"]:
                continue
            score, related = self.score(n, query, hint)
            if related or hint.get("role") or scope_ref:
                scored.append((score, n))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [self.record(n, score) for score, n in scored[:max(0, limit)]]

    def exact(self, target, *, action="click", hint=None):
        hint = hint or {}
        name = normalized(hint.get("name") or target)
        # Conservative wrappers, never a fuzzy-match click.
        if not hint.get("name"):
            name = re.sub(r"^the\s+", "", name)
            name = re.sub(r"\s+(?:button|field|textbox|checkbox)$", "", name)
        candidates = self.retrieve(target, action=action, hint=hint, limit=len(self.nodes))
        def exact_scope(record):
            requested = normalized(hint.get("scope"))
            if not requested:
                return True
            context = normalized(record.get("scope"))
            return requested == context or requested in [
                normalized(p) for p in re.split(r"\s*/\s*|\s*\|\s*", context)]
        matches = [n for n in candidates if normalized(n.get("name")) == name and exact_scope(n)]
        return matches[0] if len(matches) == 1 else None

    def record(self, node, score=None):
        out = {k: node[k] for k in ("ref", "role", "name", "value", "states", "direct",
                                   "in_viewport", "bounds", "frame_id") if k in node}
        out["scope"] = self.context(node["ref"])
        out["scope_ref"] = self.scope(node["ref"])
        if score is not None:
            out["retrieval_score"] = round(score, 4)
        return out

    def overview(self, query, limit=80, scope_ref=None):
        scope_ref = self.canonical(scope_ref)
        if scope_ref and scope_ref not in self.by_ref:
            raise ValueError("Unknown scope reference")
        ranked = []
        for n in self.nodes:
            if (n.get("role") == "generic" or n.get("structural_only")) and n["ref"] not in self.region_refs:
                continue
            if not self.within(n, scope_ref):
                continue
            score, _ = self.score(n, query)
            if n.get("role") in URGENT:
                score += 90
            elif n.get("role") == "heading":
                score += 10
            elif n.get("role") in REGIONS:
                score += 6
            # Relevant facts outrank irrelevant controls; retain a mix of regions/facts/actions.
            score += 2 * bool(n.get("direct"))
            ranked.append((score, n))
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        return [self.record(n, score) for score, n in ranked[:limit]]
