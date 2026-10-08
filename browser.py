"""Chromium AX/DOM observations and snapshot-scoped deterministic actions.

Public policy actions stay click/type/select/scroll with optional target_ref.
DOM coordinates are CSS viewport coordinates, never desktop screenshot pixels.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from .scene import Foreground, Scene, scene_from_foreground, probe_foreground, observation_scene
from .browser_index import BrowserIndex, region_summary


class BrowserActionError(RuntimeError):
    def __init__(self, message, dispatched=False):
        super().__init__(message)
        self.dispatched = dispatched


INTERACTIVE = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
               "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
               "spinbutton", "listbox", "option", "treeitem"}
SKIP = {"none", "InlineTextBox", "LineBreak"}


def keep_ax(node):
    role = node.get("role", {}).get("value")
    structural = role in {"none", "generic"} and node.get("backendDOMNodeId") and node.get("childIds")
    return bool(structural or (not node.get("ignored") and role not in SKIP))


@dataclass
class BrowserOptions:
    endpoint: str = "http://127.0.0.1:9222"
    timeout_ms: int = 4000
    max_nodes: int = 4000
    max_snapshot_age: float = 180.0
    cache_refresh_seconds: float = 1.0


class BrowserSession:
    def __init__(self, page=None, options=None, foreground_check=None, foreground_probe=None):
        self.options = options or BrowserOptions()
        self.page = page
        self.foreground_check = foreground_check
        self.foreground_probe = foreground_probe
        self._managed_page = page is not None and foreground_check is None and foreground_probe is None
        self.scene = Scene()
        self._target_id = ""
        self._snapshot_foreground = None
        self._playwright = self._browser = self._cdp = None
        self.snapshot = None
        self.nodes = {}
        self._document_id = None
        self.index = None
        self._cache_scope = self._cache_stamp = None
        self._raw_cache = None
        self._cache_time = 0.0
        self._cache_snapshot_id = None
        self._objects = {}
        self._object_group = "osworld-" + uuid.uuid4().hex
        self._watch_key = "__osworld_" + uuid.uuid4().hex
        self._focus_probes = {}

    def _real_page_focus(self, page):
        # CDP attachment may emulate focus. Desktop routing must inspect actual page focus.
        if self._browser is not None and not self._managed_page:
            if page not in self._focus_probes:
                probe = page.context.new_cdp_session(page)
                try:
                    probe.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                except Exception:
                    probe.detach()
                    raise BrowserActionError("Real browser focus could not be established")
                self._focus_probes[page] = probe
        return page.evaluate("document.hasFocus() && document.visibilityState === 'visible'")

    def _foreground(self):
        if self.foreground_probe:
            return self.foreground_probe()
        if self.foreground_check:
            return Foreground(available=True, is_browser=bool(self.foreground_check()), reason="legacy_focus_probe")
        if self._managed_page:
            return Foreground(available=True, window_id="managed_page", is_browser=True, reason="managed_browser_binding")
        return probe_foreground()

    def _same_foreground(self, before):
        current = self._foreground()
        return (current.available and current.is_browser and not current.native_ui
                and (not before or not before.window_id or before.window_id == current.window_id)
                and (not before or not before.process_id or before.process_id == current.process_id)
                and (not before or before.generation == current.generation)
                and (not before or not before.focus_id or before.focus_id == current.focus_id))

    def _attach(self):
        if self.page is None or self._browser is not None:
            if self._browser is None:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                try:
                    self._browser = self._playwright.chromium.connect_over_cdp(
                        self.options.endpoint, timeout=self.options.timeout_ms)
                except Exception:
                    self._playwright.stop()
                    self._playwright = None
                    raise
            candidates = []
            for ctx in self._browser.contexts:
                for page in ctx.pages:
                    if not page.url.startswith(("http://", "https://", "file://")):
                        continue
                    try:
                        if self._real_page_focus(page):
                            candidates.append(page)
                    except Exception:
                        continue
            if len(candidates) != 1:
                self.invalidate()
                raise BrowserActionError("No unambiguous focused browser page; use GUI")
            if self.page is not candidates[0] and self._cdp is not None:
                self._clear_cache()
                self._cdp.detach()
                self._cdp = None
            self.page = candidates[0]
        if self._cdp is None:
            self._cdp = self.page.context.new_cdp_session(self.page)
            self._cdp.send("Accessibility.enable")
            self._target_id = self._cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]

    def _document(self):
        return self._cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]

    def _clear_cache(self):
        if self._cdp and self._objects:
            try:
                self._cdp.send("Runtime.releaseObjectGroup", {"objectGroup": self._object_group})
            except Exception:
                pass
        self._objects = {}
        self._cache_scope = self._cache_stamp = self._raw_cache = None
        self._cache_snapshot_id = None
        self.index = None

    def _watch_state(self):
        # Observe DOM, input/focus events AND property changes. AX events alone miss new nodes.
        return self.page.evaluate("""key => {
          if (!window[key]) {
            const state = {revision:0, geometry:0};
            new MutationObserver(() => {state.revision++;state.geometry++;})
              .observe(document,{subtree:true,childList:true,attributes:true,characterData:true});
            for (const event of ['input','change','focusin','focusout'])
              document.addEventListener(event,()=>state.revision++,true);
            for (const event of ['scroll','resize'])
              window.addEventListener(event,()=>state.geometry++,true);
            window[key]=state;
          }
          let hash=2166136261;
          for (const e of document.querySelectorAll('input,textarea,select')) {
            const value=e.value+'|'+e.checked+'|'+e.disabled+'|'+e.readOnly;
            for(let i=0;i<value.length;i++) hash=Math.imul(hash^value.charCodeAt(i),16777619);
          }
          return {revision:window[key].revision,geometry:window[key].geometry,properties:hash>>>0};
        }""", self._watch_key)

    def collect(self):
        start = time.perf_counter()
        previous_index = self.index
        self.scene = Scene(reason="foreground_probe_pending")
        self.snapshot = None
        self.nodes = {}
        foreground = self._foreground()
        if not foreground.available or not foreground.is_browser or foreground.native_ui:
            self.invalidate()
            self.scene = scene_from_foreground(foreground)
            return None
        # Reselect focused tab, retaining the CDP session if it is still the same page.
        self.snapshot = None
        self.nodes = {}
        self.scene = scene_from_foreground(foreground)
        try:
            self._attach()
        except Exception:
            self.scene = scene_from_foreground(foreground)
            self.scene.reason = "active_page_unconfirmed"
            raise
        if not self._managed_page and not self._real_page_focus(self.page):
            self.invalidate()
            self.scene = scene_from_foreground(foreground)
            self.scene.mode = "browser_native"
            self.scene.reason = "page_content_not_focused"
            return None
        self._snapshot_foreground = foreground
        self.scene = Scene(browser_use=1, mode="browser_content", reason="ax_capture_pending",
                           foreground=scene_from_foreground(foreground).foreground, url=self.page.url)
        doc_before = self._document()
        self.scene.page_id = f"{self._target_id}/{doc_before}"
        url = self.page.url
        viewport = self.page.evaluate("({width:innerWidth,height:innerHeight,x:scrollX,y:scrollY,dpr:devicePixelRatio})")
        main_frame = self._cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        stamp = self._watch_state()
        scope = (self._target_id, doc_before, url)
        semantic_stamp = lambda s: ((s or {}).get("revision"), (s or {}).get("properties"))
        cache_hit = (self._raw_cache is not None and scope == self._cache_scope
                     and semantic_stamp(stamp) == semantic_stamp(self._cache_stamp)
                     and time.monotonic() - self._cache_time < self.options.cache_refresh_seconds)
        previous_viewport = previous_index.block.get("viewport", {}) if previous_index else {}
        geometry_changed = cache_hit and (stamp["geometry"] != self._cache_stamp["geometry"] or
            any(viewport.get(k) != previous_viewport.get(k) for k in ("x", "y", "width", "height", "dpr")))
        if cache_hit:
            raw = self._raw_cache
            if geometry_changed:
                self._cache_snapshot_id = None
                if self._objects:
                    self._cdp.send("Runtime.releaseObjectGroup", {"objectGroup": self._object_group})
                    self._objects.clear()
                self._cache_stamp = stamp
        else:
            previous_raw, previous_id = self._raw_cache, self._cache_snapshot_id
            previous_scope, previous_stamp = self._cache_scope, self._cache_stamp
            self._clear_cache()
            raw = self._cdp.send("Accessibility.getFullAXTree")["nodes"]
            after = self._watch_state()
            if stamp != after:
                raise BrowserActionError("Page changed during AX capture; observe again")
            self._raw_cache, self._cache_scope, self._cache_stamp = raw, scope, stamp
            self._cache_time = time.monotonic()
            if raw == previous_raw and scope == previous_scope and stamp == previous_stamp:
                self._cache_snapshot_id = previous_id
        snapshot_id = self._cache_snapshot_id or uuid.uuid4().hex[:12]
        self._cache_snapshot_id = snapshot_id
        retained = [n for n in raw if keep_ax(n)]
        raw_map = {n["nodeId"]: n for n in raw}
        refs = {n["nodeId"]: f"{snapshot_id}:{i}" for i, n in enumerate(retained)}
        nodes = []
        # No prefix limit: a target after node 4000 must remain retrievable locally.
        reuse_index = cache_hit and previous_index and not geometry_changed
        for n in ([] if reuse_index else retained):
            backend = n.get("backendDOMNodeId")
            role = n.get("role", {}).get("value", "")
            states = {p["name"]: p.get("value", {}).get("value") for p in n.get("properties", [])
                      if p["name"] in {"checked", "selected", "expanded", "disabled", "focused", "required", "readonly", "level"}}
            parent = n.get("parentId")
            seen = set()
            while parent and parent not in refs and parent not in seen:
                seen.add(parent)
                parent = raw_map.get(parent, {}).get("parentId")
            frame_id = n.get("frameId")
            ancestor, frame_seen = n, set()
            while not frame_id and ancestor.get("parentId") and ancestor["parentId"] not in frame_seen:
                frame_seen.add(ancestor["parentId"])
                ancestor = raw_map.get(ancestor["parentId"], {})
                frame_id = ancestor.get("frameId")
            frame_id = frame_id or main_frame
            node = {"ref": refs[n["nodeId"]], "parent": refs.get(parent), "role": role,
                    "name": str((n.get("name") or {}).get("value", ""))[:400], "states": states,
                    "structural_only": bool(n.get("ignored") or role == "none"),
                    "backend_id": backend, "frame_id": frame_id,
                    "bounds": None, "in_viewport": None,
                    "direct": bool(backend and role in INTERACTIVE and frame_id == main_frame)}
            # Text input values are read lazily with password redaction, never indexed blindly.
            if "value" in n and role not in {"textbox", "searchbox"}:
                node["value"] = str(n["value"].get("value", ""))[:400]
            nodes.append(node)
        if reuse_index:
            nodes = previous_index.nodes
        if self._document() != doc_before or self.page.url != url:
            raise BrowserActionError("Document changed during observation; retry capture")
        if not self._same_foreground(foreground) or (not self._managed_page and not self.page.evaluate("document.hasFocus() && document.visibilityState === 'visible'")):
            self.invalidate()
            self.scene = Scene(reason="focus_changed_during_capture")
            return None
        warnings = []
        if len(self.page.frames) > 1:
            warnings.append("iframe content may be incomplete; iframe actions use visual fallback")
        self._document_id = doc_before
        self.snapshot = {"kind": "browser_ax", "snapshot_id": snapshot_id, "url": url,
            "title": self.page.title(), "viewport": viewport, "nodes": nodes, "warnings": warnings,
            "cache_hit": cache_hit, "geometry_cache_invalidated": bool(geometry_changed),
            "cache_revision": stamp["revision"], "document_stamp": stamp,
            "captured_at": time.time(), "capture_ms": round((time.perf_counter() - start) * 1000, 2)}
        self.nodes = {n["ref"]: n for n in nodes}
        self.index = previous_index if reuse_index else BrowserIndex(self.snapshot)
        self.index.block = self.snapshot
        self.snapshot["capture_ms"] = round((time.perf_counter() - start) * 1000, 2)
        self.scene.structure_available = True
        self.scene.snapshot_id = snapshot_id
        self.scene.reason = "focused_page_with_ax"
        return self.snapshot

    def capture_failed(self):
        self.snapshot = None
        self.nodes = {}
        self._clear_cache()
        self.scene.structure_available = False
        self.scene.snapshot_id = ""
        if self.scene.browser_use:
            self.scene.reason = "ax_capture_failed"

    def route(self, action, observation, *, allowed_refs=None, provenance="decision"):
        raw_ref = action.args.get("target_ref")
        ref = observation.info.get("ref_aliases", {}).get(raw_ref, raw_ref)
        if not ref:
            return None
        scene = observation_scene(observation)
        if scene.get("browser_use") != 1 or not scene.get("structure_available"):
            return {"channel": "reobserve", "reason": "Browser structure is disabled in this scene"}
        allowlist = observation.info.get("exposed_refs", self.nodes) if allowed_refs is None else allowed_refs
        if ref not in allowlist:
            return {"channel": "reobserve", "reason": "target_ref was not in the policy input"}
        if not self.snapshot or ref not in self.nodes:
            return {"channel": "reobserve", "reason": "Unknown or stale target_ref"}
        if action.action not in {"click", "double_click", "right_click", "type", "select", "scroll"}:
            return None
        node = self.nodes[ref]
        if not node["direct"] and action.action != "scroll":
            return None
        # Frame coordinates are deliberately not used as top-level coordinates.
        if node.get("frame_id") != self._cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]:
            return None
        return {"channel": "browser_dom", "snapshot_id": self.snapshot["snapshot_id"],
                "target_ref": ref, "coordinate_space": "css_viewport", "provenance": provenance}

    def enrich(self, refs):
        """Lazy DOM inspection; one batched JS call after resolving uncached backend nodes."""
        if not self.snapshot or self._document() != self._document_id or self.page.url != self.snapshot["url"]:
            raise BrowserActionError("Snapshot no longer describes this document")
        if not self._same_foreground(self._snapshot_foreground):
            raise BrowserActionError("Browser lost focus")
        selected, objects = [], []
        for ref in dict.fromkeys(refs):
            node = self.nodes.get(ref)
            if not node or not node.get("backend_id"):
                continue
            backend = node["backend_id"]
            obj = self._objects.get(backend)
            if obj is None:
                obj = self._cdp.send("DOM.resolveNode", {"backendNodeId": backend,
                    "objectGroup": self._object_group})["object"].get("objectId")
                if obj:
                    self._objects[backend] = obj
            if obj:
                selected.append(node)
                objects.append(obj)
        if not objects:
            return
        result = self._cdp.send("Runtime.callFunctionOn", {
            "objectId": objects[0], "returnByValue": True,
            "arguments": [{"objectId": obj} for obj in objects],
            "functionDeclaration": """function(){return Array.from(arguments).map(e=>{
              if(!(e instanceof Element)||!e.isConnected)return {in_viewport:false};
              const r=e.getBoundingClientRect(),s=getComputedStyle(e);
              const visible=r.width>0&&r.height>0&&s.display!=='none'&&s.visibility==='visible';
              return {tag:e.tagName.toLowerCase(),bounds:[r.left+scrollX,r.top+scrollY,r.width,r.height],
                in_viewport:visible&&r.left<innerWidth&&r.right>0&&r.top<innerHeight&&r.bottom>0,
                value:e.type==='password'?'[redacted]':(e.tagName==='TEXTAREA'||(e.tagName==='INPUT'&&!['checkbox','radio','button','submit','reset','file','hidden'].includes(e.type))?String(e.value).slice(0,400):null)};
            })}"""})
        if result.get("exceptionDetails"):
            raise BrowserActionError("Candidate DOM inspection failed")
        for node, detail in zip(selected, result.get("result", {}).get("value", [])):
            node.update({k: v for k, v in detail.items() if v is not None})

    def inspect(self, query="", scope_ref=None, cursor=None, limit=40):
        if not self.scene.browser_use or not self.index:
            raise BrowserActionError("Page inspection requires browser content")
        scope_ref = self.index.canonical(scope_ref)
        if cursor:
            prefix, offset = cursor.rsplit(":", 1)
            if prefix != self.snapshot["snapshot_id"]:
                raise BrowserActionError("Inspection cursor belongs to an old snapshot")
            offset = int(offset)
        else:
            offset = 0
        if offset < 0:
            raise BrowserActionError("Invalid inspection cursor")
        ranked = self.index.overview(query, limit=len(self.nodes), scope_ref=scope_ref)
        chosen = ranked[offset:offset + limit]
        self.enrich(n["ref"] for n in chosen)
        refs = {n["ref"] for n in chosen}
        refs.update(a["ref"] for n in chosen for a in self.index.ancestors(n["ref"]))
        return {**{k: v for k, v in self.snapshot.items() if k != "nodes"},
                "kind": "browser_region", "query": query, "scope_ref": scope_ref,
                "nodes": [n for n in self.snapshot["nodes"] if n["ref"] in refs],
                "next_cursor": f"{self.snapshot['snapshot_id']}:{offset + limit}" if offset + limit < len(ranked) else None}

    def _call(self, obj, function, args=None):
        result = self._cdp.send("Runtime.callFunctionOn", {"objectId": obj,
            "functionDeclaration": function, "arguments": [{"value": a} for a in (args or [])],
            "returnByValue": True})
        if result.get("exceptionDetails"):
            raise BrowserActionError("DOM inspection failed")
        return result.get("result", {}).get("value")

    def _check(self, ref, snapshot_id):
        if (not self.snapshot or self.snapshot["snapshot_id"] != snapshot_id or ref not in self.nodes or
                time.time() - self.snapshot["captured_at"] > self.options.max_snapshot_age):
            raise BrowserActionError("Stale snapshot; observe again")
        if (not self._same_foreground(self._snapshot_foreground) or
                (not self._managed_page and not self.page.evaluate("document.hasFocus() && document.visibilityState === 'visible'"))):
            raise BrowserActionError("Browser lost focus; observe again")
        if self._document() != self._document_id or self.page.url != self.snapshot["url"]:
            raise BrowserActionError("Page navigated after observation")
        node = self.nodes[ref]
        partial = self._cdp.send("Accessibility.getPartialAXTree", {"backendNodeId": node["backend_id"], "fetchRelatives": False})
        actual = next((n for n in partial["nodes"] if n.get("backendDOMNodeId") == node["backend_id"]), {})
        if (actual.get("ignored", True) or actual.get("role", {}).get("value") != node["role"] or
                str(actual.get("name", {}).get("value", ""))[:400] != node["name"]):
            raise BrowserActionError("Target semantics changed after observation")
        states = {p["name"]: p.get("value", {}).get("value") for p in actual.get("properties", [])}
        if any(states.get(k) != node.get("states", {}).get(k) for k in ("checked", "selected", "expanded", "disabled", "readonly")):
            raise BrowserActionError("Target state changed after observation")
        if "value" in node and node["value"] != "[redacted]" and str(actual.get("value", {}).get("value", ""))[:400] != node["value"]:
            raise BrowserActionError("Target value changed after observation")
        scope_ref = self.index.scope(ref) if self.index else None
        if scope_ref:
            scope = self.nodes[scope_ref]
            if scope.get("backend_id"):
                # The partial tree only exposes raw direct children. Our index skips generic
                # wrappers, so query the smallest semantic region and flatten it identically.
                relatives = self._cdp.send("Accessibility.queryAXTree", {
                    "backendNodeId": scope["backend_id"]})["nodes"]
                current = next((n for n in relatives if n.get("backendDOMNodeId") == scope["backend_id"]), {})
                raw_by_id = {n["nodeId"]: n for n in relatives}
                retained = {n["nodeId"] for n in relatives if keep_ax(n)}
                actual_children = []
                region_nodes, region_children = {}, {}
                for child in relatives:
                    if child["nodeId"] not in retained:
                        continue
                    parent, seen = child.get("parentId"), set()
                    while parent in raw_by_id and parent not in retained and parent not in seen:
                        seen.add(parent)
                        parent = raw_by_id[parent].get("parentId")
                    region_nodes[child["nodeId"]] = {"role": child.get("role", {}).get("value"),
                        "name": str((child.get("name") or {}).get("value", ""))[:400], "parent": parent}
                    region_children.setdefault(parent, []).append(child["nodeId"])
                    if parent == current.get("nodeId"):
                        actual_children.append((child.get("backendDOMNodeId") or 0,
                            "generic" if child.get("role", {}).get("value") in {"none", "generic"} else child.get("role", {}).get("value"),
                            str((child.get("name") or {}).get("value", ""))[:400]))
                expected = [(self.nodes[c].get("backend_id") or 0,
                             "generic" if self.nodes[c]["role"] in {"none", "generic"} else self.nodes[c]["role"], self.nodes[c]["name"])
                            for c in self.index.children.get(scope_ref, [])]
                actual_role = current.get("role", {}).get("value")
                role_matches = (actual_role == scope["role"] or
                                (scope.get("structural_only") and actual_role in {"none", "generic"}))
                if ((current.get("ignored", True) and not scope.get("structural_only"))
                        or not role_matches
                        or str((current.get("name") or {}).get("value", ""))[:400] != scope["name"]
                        or sorted(actual_children)[:8] != sorted(expected)[:8]
                        or region_summary(current.get("nodeId"), region_nodes, region_children)[0] != self.index._region_text.get(scope_ref, "")
                        or node["backend_id"] not in {n.get("backendDOMNodeId") for n in relatives}):
                    raise BrowserActionError("Target region semantics changed after observation")
        obj = self._cdp.send("DOM.resolveNode", {"backendNodeId": node["backend_id"]})["object"].get("objectId")
        if not obj:
            raise BrowserActionError("Target is detached")
        return node, obj

    def execute(self, action, route):
        started, dispatched, obj = time.perf_counter(), False, None
        try:
            node, obj = self._check(route["target_ref"], route["snapshot_id"])
            self._cdp.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": node["backend_id"]})
            inspect = """function() {
              if (!this.isConnected || !(this instanceof Element)) return {error:'detached'};
              if (this.matches(':disabled') || this.getAttribute('aria-disabled') === 'true' || this.closest('[inert]')) return {error:'disabled'};
              const r=this.getBoundingClientRect(), s=getComputedStyle(this);
              if (r.width<=0 || r.height<=0 || s.visibility!=='visible' || s.display==='none') return {error:'hidden'};
              const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
              const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
              let hit=document.elementFromPoint(x,y);
              while(hit && hit.shadowRoot && hit.shadowRoot.elementFromPoint) {
                const next=hit.shadowRoot.elementFromPoint(x,y); if (!next || next===hit) break; hit=next;
              }
              if (!hit || !(this===hit || this.contains(hit))) return {error:'occluded'};
              return {x,y,w:r.width,h:r.height,tag:this.tagName.toLowerCase(),readOnly:!!this.readOnly,
                      editable:this.isContentEditable || this.tagName==='TEXTAREA' || (this.tagName==='INPUT' && !['button','submit','checkbox','radio','file'].includes(this.type))};
            }"""
            first = self._call(obj, inspect)
            self.page.wait_for_timeout(35)
            point = self._call(obj, inspect)
            if not point or point.get("error"):
                raise BrowserActionError((point or {}).get("error", "Target not actionable"))
            if first != point:
                raise BrowserActionError("Target moved during actionability check")
            name, args = action.action, action.args
            if name == "type" and (not point["editable"] or point["readOnly"]):
                raise BrowserActionError("Target is not editable")
            if name == "select" and point["tag"] != "select":
                raise BrowserActionError("Custom dropdown: use click and then click its option")
            if name == "select":
                matches = self._call(obj, "function(label){return Array.from(this.options).filter(o=>o.label===label && !o.disabled).map(o=>o.index)}", [str(args["option"])])
                if len(matches) != 1:
                    raise BrowserActionError("Select option is absent or ambiguous")
                dispatched = True
                self._call(obj, """function(i){ this.selectedIndex=i; this.dispatchEvent(new Event('input',{bubbles:true})); this.dispatchEvent(new Event('change',{bubbles:true})); }""", [matches[0]])
            elif name == "scroll":
                dispatched = True
                self.page.mouse.move(point["x"], point["y"])
                self.page.mouse.wheel(0, int(args["amount"]) * 120 * (-1 if args["direction"] == "up" else 1))
            else:
                dispatched = True
                self.page.mouse.click(point["x"], point["y"], button="right" if name == "right_click" else "left",
                                      click_count=2 if name == "double_click" else 1)
                if name == "type":
                    # Focus may be stolen by a click handler. Never type into a different node.
                    if not self._call(obj, "function(){let a=document.activeElement;while(a&&a.shadowRoot&&a.shadowRoot.activeElement)a=a.shadowRoot.activeElement;return a===this || this.contains(a)}"):
                        raise BrowserActionError("Focus changed after click", dispatched=True)
                    if args.get("overwrite"):
                        self.page.keyboard.press("ControlOrMeta+A")
                        self.page.keyboard.press("Backspace")
                    self.page.keyboard.insert_text(str(args["text"]))
            return {"status": "executed", "dispatched": True, "channel": "browser_dom",
                    "target_ref": route["target_ref"], "coord": [point["x"], point["y"]],
                    "coordinate_space": "css_viewport", "latency_ms": round((time.perf_counter()-started)*1000, 2)}
        except Exception as exc:
            # Never repeat an uncertain side effect through a different execution channel.
            return {"status": "uncertain" if dispatched else "rejected", "dispatched": dispatched,
                    "channel": "browser_dom", "error": str(exc)[:500],
                    "latency_ms": round((time.perf_counter()-started)*1000, 2)}
        finally:
            if obj:
                try:
                    self._cdp.send("Runtime.releaseObject", {"objectId": obj})
                except Exception:
                    pass

    def source(self, ref=None, max_chars=12000):
        """Sanitized rendered DOM only: scripts/style/hidden fields are excluded."""
        if not self.scene.browser_use or not self._same_foreground(self._snapshot_foreground):
            raise BrowserActionError("Page source is disabled outside browser content")
        if not self.page or (not self._managed_page and not self.page.evaluate("document.hasFocus() && document.visibilityState === 'visible'")):
            raise BrowserActionError("Page source requires focused content")
        if self.page.url != self.scene.url or (self._document_id is not None and self._document() != self._document_id):
            raise BrowserActionError("Page changed before source request")
        fn = """function(limit){
          let out='',visits=0;const esc=s=>s.replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
          const add=s=>{out+=s.slice(0,Math.max(0,limit-out.length))};
          function walk(n,depth){
            if(out.length>=limit||visits++>=2000||depth>64)return;
            if(n.nodeType===3){add(esc(n.textContent||''));return}
            if(!(n instanceof Element)||n.matches('script,style,noscript,template,input[type=hidden],[hidden],[aria-hidden=true]'))return;
            const tag=n.tagName.toLowerCase();add('<'+tag);
            for(const a of n.attributes)if(['id','role','aria-label','href','type','name','placeholder'].includes(a.name))add(' '+a.name+'="'+esc(a.value)+'"');
            add('>');for(const child of n.childNodes){if(out.length>=limit)break;walk(child,depth+1)}add('</'+tag+'>');
          }walk(this,0);return out;
        }"""
        if ref:
            _, obj = self._check(ref, self.snapshot["snapshot_id"])
            try:
                return self._call(obj, fn, [max_chars])
            finally:
                self._cdp.send("Runtime.releaseObject", {"objectId": obj})
        return self.page.locator("body").evaluate("(el,limit)=>(" + fn + ").call(el,limit)", max_chars)

    def invalidate(self):
        self._clear_cache()
        if self._cdp is not None:
            try:
                self._cdp.detach()
            except Exception:
                pass
        self._cdp = None
        if self._browser is not None:
            self.page = None
        self.snapshot = None
        self.nodes = {}
        self.scene = Scene(reason="invalidated")
        self._snapshot_foreground = None

    def close(self):
        self.invalidate()
        for probe in self._focus_probes.values():
            try:
                probe.detach()
            except Exception:
                pass
        self._focus_probes.clear()
        if self._playwright is not None:
            self._playwright.stop()  # disconnect; do not close the user's existing browser
            self._playwright = self._browser = None
