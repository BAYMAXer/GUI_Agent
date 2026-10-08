"""VMware 环境适配器：把抽象 Environment 接到 OSWorld 的 DesktopEnv + VMware 虚拟机。

关键点：
- 包装 OSWorld 的 DesktopEnv（provider=vmware, action_space=pyautogui）；
- 把新框架的 Action（已坐标化）翻译成 pyautogui 命令字符串（to_pyautogui）；
- 一个 VM 环境实例对应一个 task（reset 会做任务 setup + 快照回退）。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Optional

from ..actions import Action
from ..env import Environment, Observation, StepResult, run_local_command, run_local_python
from .uno_commands import SET_CELL_VALUES_CMD, SAVE_DOC_CMD


def to_pyautogui(action: Action) -> str:
    """把新框架的 Action（env 类，已坐标化）翻译成 OSWorld 的 pyautogui 命令字符串。"""
    name = action.action
    a = action.args
    head = "import pyautogui; import time; "

    # grounding 失败：拒绝执行（返回空命令 → step() 空转一步），绝不做坐标钳制。
    if a.get("grounding_failed"):
        return ""

    if name == "click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            # 对 Calc 单元格：双击确保焦点真正切换（单次 click 可能只是激活表格区，不切选中格）。
            # 判断：target 是单元格引用（如 E3 / Sheet1 的 A5）或描述里含"单元格"。
            t = str(a.get("target", "")).lower()
            import re
            if re.fullmatch(r"[a-z]\d+", t.strip()) or "单元格" in a.get("target", ""):
                return f"{head} pyautogui.click({x}, {y}); time.sleep(0.2); pyautogui.click({x}, {y})"
            return f"{head} pyautogui.click({x}, {y})"
        return ""  # grounding 失败：不执行，空转
    if name == "double_click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            return f"{head} pyautogui.doubleClick({x}, {y})"
        return ""
    if name == "right_click":
        x, y = a.get('x'), a.get('y')
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            return f"{head} pyautogui.click({x}, {y}, button='right')"
        return ""
    if name == "type":
        # 原子「聚焦+输入」：先点击目标元素聚焦，再输入文字（对齐 Agent S3 的 type 语义）。
        x, y = a.get('x'), a.get('y')
        cmd = head
        if x is not None and y is not None and int(x) >= 0 and int(y) >= 0:
            cmd += f"pyautogui.click({x}, {y}); "
        if a.get('overwrite'):
            # 覆盖写：全选 + 退格删除旧内容，再输入新文字（对齐 Agent S3 的 type(overwrite=True)）。
            cmd += "pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace'); "
        cmd += f"pyautogui.write({str(a.get('text', ''))!r})"
        return cmd
    if name == "scroll":
        amount = int(a.get("amount", 1))
        if a.get("direction", "down") == "up":
            pass
        else:
            amount = -amount
        return f"{head} pyautogui.scroll({amount})"
    if name == "press":
        return f"{head} pyautogui.press({str(a.get('key', ''))!r})"
    if name == "hotkey":
        keys = list(a.get("keys", []))
        return f"{head} pyautogui.hotkey(*{keys!r})"
    if name == "goto":
        url = str(a.get("url", ""))
        return (f"{head} pyautogui.hotkey('ctrl', 'l'); time.sleep(0.5); "
                f"pyautogui.write({url!r}); pyautogui.press('enter')")
    if name == "drag_and_drop":
        return (f"{head} pyautogui.moveTo({a.get('x1')}, {a.get('y1')}); "
                f"pyautogui.dragTo({a.get('x2')}, {a.get('y2')}, duration=1.0)")
    if name == "select":
        # 简化：点开下拉框 → 输入选项 → 回车
        return (f"{head} pyautogui.click({a.get('x')}, {a.get('y')}); time.sleep(0.5); "
                f"pyautogui.write({str(a.get('option', ''))!r}); pyautogui.press('enter')")
    if name == "wait":
        return f"{head} time.sleep({float(a.get('seconds', 1))})"
    if name == "save":
        # 程序化保存（UNO store），避开 Ctrl+S 对 xlsx/docx/pptx 弹的"确认格式"对话框
        app_name = a.get("app_name")
        return SAVE_DOC_CMD.format(app_name=repr(app_name) if app_name else "None")
    if name == "switch_applications":
        # 简化：alt+tab 切应用（要按 app_code 精确定位需扩展）
        return f"{head} pyautogui.hotkey('alt', 'tab')"
    if name == "set_cell_values":
        # LibreOffice UNO 直写单元格（不经 GUI），写完后 store() 落盘
        cell_values = a.get("new_cell_values") or {}
        app_name = str(a.get("app_name") or "")
        sheet_name = str(a.get("sheet_name") or "Sheet1")
        return SET_CELL_VALUES_CMD.format(
            cell_values=cell_values, app_name=app_name, sheet_name=sheet_name)
    return ""


def verify_cell_values(env, cell_values, app_name, sheet_name):
    """写完后校验：读目标单元格，确认真的写进去了。返回 (bool, 说明)。"""
    from .uno_commands import VERIFY_CELL_CMD
    ctrl = getattr(env, "controller", None)
    if ctrl is None:
        return False, "无 controller，无法校验"
    try:
        result = ctrl.run_python_script(VERIFY_CELL_CMD.format(
            cell_ref=list(cell_values.keys())[0] if cell_values else "",
            app_name=app_name or "",
            sheet_name=sheet_name or "Sheet1",
        ))
        out = ""
        if isinstance(result, dict):
            out = (result.get("output") or "").strip()
        elif isinstance(result, str):
            out = result.strip()
        if not out:
            return False, "校验脚本无输出"
        # 校验：返回里应有 CELL_VALUE=...（非 None/空）
        if "CELL_VALUE=" in out and ("None" not in out.split("CELL_VALUE=")[1][:20] or "'" in out.split("CELL_VALUE=")[1][:20]):
            return True, out
        return False, out
    except Exception as exc:  # noqa: BLE001
        return False, f"校验失败: {exc}"


class VMwareEnvironment(Environment):
    """包装 OSWorld DesktopEnv 的 VMware 环境。"""

    def __init__(self, task_config: Dict[str, Any], vm_path: Optional[str] = None,
                 headless: bool = False, screen_width: int = 1920,
                 screen_height: int = 1080, desktop_env_path: Optional[str] = None,
                 snapshot_name: Optional[str] = None, provider: str = "vmware",
                 cache_dir: Optional[str] = None, decision_model=None):
        self.task_config = task_config
        self.os_name = "linux"           # Ubuntu 虚拟机
        self.screen_width = screen_width
        self.screen_height = screen_height
        # 决策模型（用于 type 落点校验，可选；None 则跳过校验）
        self.decision_model = decision_model
        # 最近一次 type 输入的文字（供 type 落点校验）
        self._last_typed_text: str = ""

        if desktop_env_path:
            sys.path.insert(0, desktop_env_path)

        from desktop_env.desktop_env import DesktopEnv  # noqa: E402
        env_kwargs: Dict[str, Any] = dict(
            provider_name=provider,
            path_to_vm=vm_path,
            snapshot_name=snapshot_name or os.getenv("OSWORLD_SNAPSHOT_NAME", "init_state_ca3"),
            action_space="pyautogui",
            screen_size=(screen_width, screen_height),
            headless=headless,
            os_type="Ubuntu",
            require_a11y_tree=False,
            enable_proxy=True,
        )
        if cache_dir:
            env_kwargs["cache_dir"] = cache_dir
        self._env = DesktopEnv(**env_kwargs)

    @property
    def controller(self):
        return getattr(self._env, "controller", None)

    def _focus_is_browser(self) -> bool:
        foreground = self._foreground()
        return foreground.available and foreground.is_browser and not foreground.native_ui

    def _foreground(self):
        from ..scene import remote_linux_probe
        return remote_linux_probe(self.controller)

    def _page_a11y(self) -> str:
        """抓当前浏览器页面的 AX-Tree（CDP Accessibility.getFullAXTree）。

        只有「焦点在浏览器」时才抓——浏览器开着但焦点不在，说明任务不在浏览器做，
        抓页面源码是错的（白抓且可能误导决策模型）。
        """
        if not self._focus_is_browser():
            return ""
        host = getattr(self._env, "vm_ip", None)
        port = getattr(self._env, "chromium_port", None) or 9222
        if not host:
            return ""
        try:
            from ..utils import fetch_accessibility_tree
            return fetch_accessibility_tree(host, port)
        except Exception:  # noqa: BLE001
            return ""

    def _a11y_text(self, obs) -> str:
        """Legacy observation data; the scene gate excludes this from visual-only policy input."""
        if not isinstance(obs, dict):
            return ""
        at = obs.get("accessibility_tree")
        if not at:
            return ""
        s = at if isinstance(at, str) else str(at)
        if len(s) > 8000:
            return s[:8000] + "\n...(AX-Tree 过长已截断)"
        return s

    def _structured_observation(self, screenshot, text=""):
        """One persistent CDP session; desktop screenshots retain desktop coordinates."""
        from ..scene import Scene
        observation = Observation(screenshot=screenshot, text=text,
                                  info={"coordinate_space": "desktop_pixels", "scene": Scene(reason="no_cdp_endpoint").to_dict()})
        host = getattr(self._env, "vm_ip", None)
        if not host:
            return observation
        session = getattr(self, "_browser_session", None)
        try:
            from ..browser import BrowserSession, BrowserOptions
            endpoint = f"http://{host}:{getattr(self._env, 'chromium_port', None) or 9222}"
            if session is None or session.options.endpoint != endpoint:
                if session:
                    session.close()
                session = self._browser_session = BrowserSession(
                    options=BrowserOptions(endpoint=endpoint), foreground_probe=self._foreground)
            block = session.collect()
            if block:
                observation.context = [block]
        except Exception as exc:
            if session:
                session.capture_failed()
            observation.info["structure_error"] = str(exc)[:300]
        if session:
            observation.info["scene"] = session.scene.to_dict()
        return observation

    def observe(self):
        obs = self._env._get_obs()
        return self._structured_observation(obs.get("screenshot"), self._a11y_text(obs))

    def route_action(self, action, observation):
        session = getattr(self, "_browser_session", None)
        return session.route(action, observation) if session and observation.browser_use else None

    def execute_routed(self, action, route):
        info = self._browser_session.execute(action, route)
        # CDP itself dispatches in CSS coordinates; no desktop/DPI offset conversion.
        time.sleep(0.12)
        return StepResult(self.observe(), info=info)

    def get_source_for_action(self, action):
        session = getattr(self, "_browser_session", None)
        if session and session.snapshot:
            try:
                return session.source(action.args.get("target_ref"))
            except Exception as exc:
                return f"DOM unavailable: {str(exc)[:300]}"
        return "当前没有有效的浏览器结构观测"

    # ---- Environment 接口 ----
    def reset(self, task: str) -> Observation:
        session = getattr(self, "_browser_session", None)
        if session:
            session.close()
            self._browser_session = None
        obs = self._env.reset(self.task_config)
        screenshot = obs.get("screenshot") if isinstance(obs, dict) else None
        a11y = self._a11y_text(obs)
        # launch 是异步的（服务器端 subprocess.Popen 立即返回），reset 返回的截图
        # 可能早于目标应用窗口出现，导致 agent 第一步面对"空桌面"而走错路。
        # 对含 launch 步骤的任务，等屏幕稳定（窗口已弹出且动画结束）后再返回截图。
        if self._has_launch_step():
            screenshot = self._wait_until_screen_stable(screenshot)
        else:
            # 对齐 Agent S3 的 reset 后 time.sleep(10)：等环境（应用启动/文件打开）
            # 完全 ready 再截图，否则初始截图是"半成品"状态会误导后续每一步决策。
            time.sleep(10)
            try:
                fresh = self._env._get_obs()
                screenshot = fresh.get("screenshot") if isinstance(fresh, dict) else screenshot
                a11y = self._a11y_text(fresh)
            except Exception:  # noqa: BLE001  截图取不到就退回 reset 返回的截图
                pass
        return self._structured_observation(screenshot, a11y)

    def _has_launch_step(self) -> bool:
        """任务 setup 里是否含 launch 步骤（含则说明有应用是异步启动的）。"""
        for cfg in self.task_config.get("config", []) or []:
            if cfg.get("type") == "launch":
                return True
        return False

    def _wait_until_screen_stable(self, initial, max_wait: float = 15.0,
                                  stable_frames: int = 3, interval: float = 0.6):
        """轮询截图，直到连续 stable_frames 帧几乎无变化（应用窗口已稳定），或超时。

        用 64x64 灰度 mean-abs-diff 判定"无变化"；截图取不到或超时就退回当前已有截图，
        保证不会比直接返回更差。
        """
        ctrl = self.controller
        if ctrl is None:
            return initial
        get_shot = getattr(ctrl, "get_screenshot", None)
        if get_shot is None:
            return initial
        try:
            from PIL import ImageChops
        except ImportError:
            return initial

        def diff(a, b):
            try:
                ia = a.convert("L").resize((64, 64))
                ib = b.convert("L").resize((64, 64))
                hist = ImageChops.difference(ia, ib).histogram()
                return sum(i * c for i, c in enumerate(hist)) / (64 * 64)
            except Exception:
                return 255.0

        prev = initial
        if prev is None:
            try:
                prev = get_shot()
            except Exception:
                return initial

        stable = 0
        elapsed = 0.0
        while elapsed < max_wait:
            time.sleep(interval)
            elapsed += interval
            try:
                cur = get_shot()
            except Exception:
                continue
            if cur is None:
                continue
            if diff(prev, cur) < 2.0:
                stable += 1
            else:
                stable = 0
            prev = cur
            if stable >= stable_frames:
                return cur
        return prev if prev is not None else initial

    def step(self, action: Action) -> StepResult:
        # set_cell_values / save 是 LibreOffice UNO 多行脚本（含 def/for/缩进），
        # 必须走 controller.run_python_script 保留多行结构。走 env.step 会被
        # execute_python_command 拼成 "python -c" 单行，破坏缩进结构导致静默失败
        # （表现为"写入校验：失败"、单元格写不进去）。
        if action.action in ("set_cell_values", "save"):
            cmd = to_pyautogui(action)
            ctrl = self.controller
            if ctrl is not None and hasattr(ctrl, "run_python_script"):
                try:
                    result = ctrl.run_python_script(cmd)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": str(exc)}
                obs = self._env._get_obs() if hasattr(self._env, "_get_obs") else {"screenshot": None}
                screenshot = obs.get("screenshot") if isinstance(obs, dict) else None
                return StepResult(
                    observation=self._structured_observation(screenshot, self._a11y_text(obs)),
                    info={"uno_result": result},
                )
            # 无 controller 时回退到 env.step
        cmd = to_pyautogui(action)
        if not cmd:
            # grounding 失败（坐标 -1）或未实现动作：空转一步，不执行 pyautogui
            import time as _t
            _t.sleep(1.0)
            obs = self._env._get_obs() if hasattr(self._env, "_get_obs") else {"screenshot": None}
            return StepResult(observation=self._structured_observation(obs.get("screenshot") if isinstance(obs, dict) else None, self._a11y_text(obs)))
        obs, reward, done, info = self._env.step(cmd, pause=3)
        screenshot = obs.get("screenshot") if isinstance(obs, dict) else None
        info = info or {}

        # type 落点校验：输入后让决策模型看一眼截图，确认文字打进了预期的输入框。
        # 典型错误：Find/Replace 场景下 grounding 把 Replace 框定位回 Find 框，
        # 导致 "text"+"test" 都打进 Find 框变成 "texttest"。校验失败就把标记写进 info，
        # pipeline 会把它暴露给模型，并提示先撤销（Ctrl+Z）再正确聚焦重输。
        if action.action == "type":
            typed = str(action.args.get("text", ""))
            if typed:
                warn = self._check_type_target(screenshot, typed)
                if warn:
                    info["type_wrong_target"] = True
                    info["type_check"] = warn
            self._last_typed_text = typed

        return StepResult(observation=self._structured_observation(screenshot, self._a11y_text(obs)),
                          reward=float(reward or 0.0), done=bool(done), info=info)

    def _check_type_target(self, screenshot, typed: str) -> Optional[str]:
        """type 后的落点校验：判断输入的文字是否出现在合理的位置。

        返回 None 表示「看着没问题」（放行）；返回一段中文说明表示「疑似打错框」。
        依赖决策模型的视觉能力；模型不可用或截图缺失时一律放行（不误伤、不阻塞主流程）。
        """
        if self.decision_model is None or screenshot is None:
            return None
        prompt = (
            "用户刚在一个输入框里输入了文字「%s」。请仔细看这张截图：\n"
            "1. 找到这段文字「%s」现在出现在屏幕上的哪个输入框/控件里；\n"
            "2. 判断它是否出现在【合理的目标位置】。最典型的错误是：做查找替换时，"
            "本该输入到「Replace/替换为」框的文字，被输入到了「Find/查找」框里"
            "（症状：Find 框里的内容变成了「查找词+刚输入的文字」拼在一起，如 texttest）。\n"
            "只输出一个词：OK（位置合理）或 WRONG（位置错误/打错框）。"
            % (typed, typed)
        )
        try:
            from ..model.decision_model import encode_image
            url = encode_image(screenshot)
            if not url:
                return None
            messages = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": prompt},
            ]}]
            raw = self.decision_model.chat(messages, retries=1)
            if "WRONG" in (raw or "").upper():
                return ("输入后检测到文字可能打进了错误的输入框（常见：查找词被拼进了查找框）。"
                        "请先用 hotkey [\"ctrl\",\"z\"] 撤销这次错误输入，"
                        "再确认焦点在正确的输入框（如 Replace 框）后重新输入。")
        except Exception:  # noqa: BLE001  校验失败放行，不影响主流程
            return None
        return None

    def run_command(self, command: str) -> str:
        ctrl = self.controller
        if ctrl is not None:
            runner = getattr(ctrl, "run_bash_script", None)
            if runner is not None:
                try:
                    result = runner(command)
                    if isinstance(result, str):
                        return result.strip() or "(无输出)"
                    if isinstance(result, dict):
                        out = (result.get("output") or "").strip()
                        err = (result.get("error") or "").strip()
                        return (out + ("\n" + err if err else "")).strip() or "(无输出)"
                except Exception:  # noqa: BLE001
                    pass
        return run_local_command(command, "linux")

    def run_python(self, code: str) -> str:
        ctrl = self.controller
        if ctrl is not None:
            runner = getattr(ctrl, "run_python_script", None)
            if runner is not None:
                try:
                    result = runner(code)
                    if isinstance(result, str):
                        return result.strip() or "(无输出)"
                    if isinstance(result, dict):
                        out = (result.get("output") or "").strip()
                        err = (result.get("error") or "").strip()
                        return (out + ("\n" + err if err else "")).strip() or "(无输出)"
                except Exception:  # noqa: BLE001
                    pass
        return run_local_python(code)

    def get_page_source(self) -> str:
        """Use the same focused-page snapshot/session as policy observations."""
        return self.get_source_for_action(Action("get_page_source"))

    def evaluate(self) -> float:
        return float(self._env.evaluate())

    def close(self) -> None:
        session = getattr(self, "_browser_session", None)
        if session:
            session.close()
        if self._env is not None:
            try:
                self._env.close()
            except Exception:  # noqa: BLE001
                pass
