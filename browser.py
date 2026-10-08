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


class BrowserActionError(RuntimeError):
    def __init__(self, message, dispatched=False):
        super().__init__(message)
        self.dispatched = dispatched


INTERACTIVE = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
               "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
               "spinbutton", "listbox", "option", "treeitem"}
SKIP = {"none", "generic", "InlineTextBox", "LineBreak"}


@dataclass
class BrowserOptions:
    endpoint: str = "http://127.0.0.1:9222"
    timeout_ms: int = 4000
    max_nodes: int = 4000
    max_snapshot_age: float = 180.0


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
                and (not before or not before.process_id or before.process_id == current.process_id))

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
                        if page.evaluate("document.hasFocus() && document.visibilityState === 'visible'"):
                            candidates.append(page)
                    except Exception:
                        continue
            if len(candidates) != 1:
                self.invalidate()
                raise BrowserActionError("No unambiguous focused browser page; use GUI")
            if self.page is not candidates[0] and self._cdp is not None:
                self._cdp.detach()
                self._cdp = None
            self.page = candidates[0]
        if self._cdp is None:
            self._cdp = self.page.context.new_cdp_session(self.page)
            self._cdp.send("Accessibility.enable")
            self._target_id = self._cdp.send("Target.getTargetInfo")["targetInfo"]["targetId"]

    def _document(self):
        return self._cdp.send("DOM.getDocument", {"depth": 0})["root"]["backendNodeId"]

    def collect(self):
        start = time.perf_counter()
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
        if not self._managed_page and not self.page.evaluate("document.hasFocus() && document.visibilityState === 'visible'"):
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
        dom = self._cdp.send("DOMSnapshot.captureSnapshot", {"computedStyles": []})
        strings, layout = dom["strings"], {}
        for doc in dom["documents"]:
            dn = doc["nodes"]
            li = doc.get("layout", {})
            bounds = dict(zip(li.get("nodeIndex", []), li.get("bounds", [])))
            for i, backend in enumerate(dn.get("backendNodeId", [])):
                attrs = dn.get("attributes", [])[i]
                at = {strings[attrs[k]]: strings[attrs[k + 1]] for k in range(0, len(attrs), 2)}
                layout[backend] = {"bounds": bounds.get(i), "tag": strings[dn["nodeName"][i]].lower(),
                                   "attrs": at, "frame_id": strings[doc["frameId"]]}
        main_frame = self._cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        raw = self._cdp.send("Accessibility.getFullAXTree")["nodes"]
        snapshot_id = uuid.uuid4().hex[:12]
        retained = [n for n in raw if not n.get("ignored") and n.get("role", {}).get("value") not in SKIP]
        raw_map = {n["nodeId"]: n for n in raw}
        refs = {n["nodeId"]: f"{snapshot_id}:{i}" for i, n in enumerate(retained)}
        nodes = []
        for n in retained[:self.options.max_nodes]:
            backend = n.get("backendDOMNodeId")
            detail = layout.get(backend, {})
            attrs = detail.get("attrs", {})
            role = n.get("role", {}).get("value", "")
            states = {p["name"]: p.get("value", {}).get("value") for p in n.get("properties", [])
                      if p["name"] in {"checked", "selected", "expanded", "disabled", "focused", "required", "readonly", "level"}}
            parent = n.get("parentId")
            seen = set()
            while parent and parent not in refs and parent not in seen:
                seen.add(parent)
                parent = raw_map.get(parent, {}).get("parentId")
            bounds = detail.get("bounds")
            visible = bool(bounds and bounds[2] > 0 and bounds[3] > 0 and
                bounds[0] < viewport["x"] + viewport["width"] and bounds[0] + bounds[2] > viewport["x"] and
                bounds[1] < viewport["y"] + viewport["height"] and bounds[1] + bounds[3] > viewport["y"])
            node = {"ref": refs[n["nodeId"]], "parent": refs.get(parent), "role": role,
                    "name": str(n.get("name", {}).get("value", ""))[:400], "states": states,
                    "backend_id": backend, "frame_id": detail.get("frame_id"),
                    "tag": detail.get("tag", ""), "bounds": bounds, "in_viewport": visible,
                    "direct": bool(backend and role in INTERACTIVE and detail.get("frame_id") == main_frame)}
            if "value" in n:
                node["value"] = "[redacted]" if attrs.get("type") == "password" else str(n["value"].get("value", ""))[:400]
            if attrs.get("href"):
                node["href"] = attrs["href"][:500]
            nodes.append(node)
        if self._document() != doc_before or self.page.url != url:
            raise BrowserActionError("Document changed during observation; retry capture")
        if not self._same_foreground(foreground) or (not self._managed_page and not self.page.evaluate("document.hasFocus() && document.visibilityState === 'visible'")):
            self.invalidate()
            self.scene = Scene(reason="focus_changed_during_capture")
            return None
        warnings = []
        if len(retained) > self.options.max_nodes:
            warnings.append("node_limit: additional nodes omitted")
        if len(self.page.frames) > 1:
            warnings.append("iframe content may be incomplete; iframe actions use visual fallback")
        self._document_id = doc_before
        self.snapshot = {"kind": "browser_ax", "snapshot_id": snapshot_id, "url": url,
            "title": self.page.title(), "viewport": viewport, "nodes": nodes, "warnings": warnings,
            "captured_at": time.time(), "capture_ms": round((time.perf_counter() - start) * 1000, 2)}
        self.nodes = {n["ref"]: n for n in nodes}
        self.scene.structure_available = True
        self.scene.snapshot_id = snapshot_id
        self.scene.reason = "focused_page_with_ax"
        return self.snapshot

    def capture_failed(self):
        self.snapshot = None
        self.nodes = {}
        self.scene.structure_available = False
        self.scene.snapshot_id = ""
        if self.scene.browser_use:
            self.scene.reason = "ax_capture_failed"

    def route(self, action, observation):
        ref = action.args.get("target_ref")
        if not ref:
            return None
        scene = observation_scene(observation)
        if scene.get("browser_use") != 1 or not scene.get("structure_available"):
            return {"channel": "reobserve", "reason": "Browser structure is disabled in this scene"}
        if ref not in observation.info.get("exposed_refs", self.nodes):
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
                "target_ref": ref, "coordinate_space": "css_viewport"}

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
        fn = """function(limit){const clone=this.cloneNode(true);if(!(clone instanceof Element))return (clone.textContent||'').slice(0,limit);if(clone.matches('script,style,noscript,template,input[type=hidden]'))return '';clone.querySelectorAll('script,style,noscript,template,input[type=hidden]').forEach(n=>n.remove());[clone,...clone.querySelectorAll('*')].forEach(n=>{for(const a of [...n.attributes]) if(!['id','role','aria-label','href','type','name','placeholder'].includes(a.name)) n.removeAttribute(a.name)});return clone.outerHTML.slice(0,limit)}"""
        if ref:
            _, obj = self._check(ref, self.snapshot["snapshot_id"])
            try:
                return self._call(obj, fn, [max_chars])
            finally:
                self._cdp.send("Runtime.releaseObject", {"objectId": obj})
        return self.page.locator("body").evaluate("(el,limit)=>(" + fn + ").call(el,limit)", max_chars)

    def invalidate(self):
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
        if self._playwright is not None:
            self._playwright.stop()  # disconnect; do not close the user's existing browser
            self._playwright = self._browser = None
