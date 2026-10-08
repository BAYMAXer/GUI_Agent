# Browser use 场景 flag：评估与实现

“按场景决定是否传网页结构”的思路合理。鼠标位置不适合做唯一判据：悬停在后台浏览器不代表它接收输入，键盘切换应用后鼠标也可能没动；Chrome 的地址栏、浏览器菜单和原生文件对话框没有可用的网页 DOM。光标如果指文本输入焦点，比鼠标悬停更接近需求，但仍要确认它属于网页内容。

本次按已给出的改进方案实现：**前台进程/窗口 + 当前网页焦点确认 + 网页结构可用性**。检测由程序完成，不额外调用模型。

## 状态与输入规则

统一状态保存在 `Observation.info["scene"]`，轨迹同样保存这里的字段：

```json
{
  "browser_use": 1,
  "structure_available": true,
  "mode": "browser_content",
  "reason": "focused_page_with_ax",
  "foreground": {"available": true, "window_id": "...", "process_name": "chrome.exe"},
  "page_id": "当前target/document身份",
  "url": "当前URL",
  "snapshot_id": "当前AX快照身份"
}
```

| 条件 | browser_use | structure_available | 本轮环境输入 |
|---|---:|---:|---|
| 普通桌面应用在前台 | 0 | false | 截图 |
| 浏览器原生菜单、对话框或网页未获焦点 | 0 | false | 截图 |
| 无法可靠确认前台/活动页，包括 CDP 连接不可用或多页歧义 | 0 | false | 截图，附简短状态原因 |
| 网页内容获焦点且 AX 抓取成功 | 1 | true | 截图 + 预算内的 AX/DOM |
| 网页内容已确认，随后 AX 抓取失败 | 1 | false | 截图，记录结构抓取失败 |

这里“仅截图”指环境感知方式。任务、记忆、工具结果、动作说明和少量程序状态仍保留，不是把整份 prompt 变成只有一张图。

`mode` 为 desktop / browser_content / browser_native / unknown；`reason` 用于区分桌面、原生界面、识别失败、AX 失败，避免把全部 false 状态混为一谈。不要求模型输出或修改 flag。

## 一次任务中的切换

每次观察都重新计算，不把 browser_use 当成一旦置 1 就永久保持的变量：

1. 桌面操作：flag=0，不连接 CDP 抓 AX，使用视觉定位。
2. 打开或切换到浏览器：下一次观察确认前台和网页焦点，再置 1，抓取结构。
3. 地址栏/原生弹窗或切回普通应用：flag=0，立即失效旧 snapshot 和节点引用。
4. 回到网页：重新抓取并分配当前快照引用，不复用离开前的引用。

源码工具结果绑定 `(page_id, url)`，仅同一网页且结构仍可用时注入一次。退出网页、切页或导航后不把旧源码带到新一轮。以前已记住的业务事实仍可留在记忆中，例如复制到另一个应用所需的信息。

AX 抓取前后检查焦点；DOM 动作执行前再检查前台窗口、进程、网页焦点与节点状态。如果模型思考期间用户切换了应用，拒绝旧操作并重新观察。当前纯视觉执行器沿用原框架；没有声称所有视觉操作已经实现原子级焦点锁定。

## 代码位置

- `scene.py`：前台信息采集、Scene 字段、输入过滤函数。
- `browser.py`：确认当前网页、维护 scene、捕获失败降级、执行前重检。
- `pipeline.py`：最终输入门控与执行路由。flag=0 时即使适配器误带旧 AX，也不会注入；普通场景的 desktop accessibility 文本同样不再注入。完成核验使用相同的结构过滤。
- `adapters/vmware_env.py`：在目标 VM 检测前台进程，连接浏览器；Docker 继承这套逻辑。
- `adapters/browser_env.py`：保存 browser-only 环境的 scene 和捕获结果。
- `utils.py`：旧的前台检测入口转接到新实现，不再按标题关键词识别。
- `tests/test_scene.py`：场景切换、输入过滤和执行路由回归测试。

## 检测依据与范围

Windows 使用 `GetForegroundWindow -> GetWindowThreadProcessId -> QueryFullProcessImageNameW` 得到进程身份，使用窗口类和 `GetGUIThreadInfo` 排除可识别的原生菜单/对话框。不能只凭 `Chrome_WidgetWin_1` 判断浏览器：其他 Chromium/Electron 应用也可能使用此类名。本机实测当前 `chatgpt.exe` 窗口就属于这种情况，现判定为普通桌面应用。

网页侧要求 `document.hasFocus()` 和 `visibilityState === "visible"`，多页无法唯一确认时不猜“最后一个 tab”。后台页有 activeElement 不代表网页拥有输入焦点。

参考官方 API：

- [GetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow)
- [GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid)
- [QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)
- [GUITHREADINFO 的菜单状态](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-guithreadinfo)
- [Document.hasFocus](https://developer.mozilla.org/en-US/docs/Web/API/Document/hasFocus)

Linux VM 使用目标机的 X11 活动窗口、PID、`/proc/PID/exe` 和窗口类型；远端检测失败不回退到查询 agent 宿主机。Wayland、macOS 和无法读取的前台状态暂保守退为 unknown/纯视觉。Firefox 等进程可识别，但当前结构采集器只接 Chromium CDP，不支持的浏览器保持视觉路径。

专用 `BrowserEnvironment` 启动的受控页面（包括 headless）属于明确绑定的 browser-only 环境，可直接把绑定页面视为输入目标；它不是 Windows 全桌面环境。连接已有浏览器的模式仍进行系统前台检测。要在 Windows 上跨多个原生应用实际操作，仍需一个提供完整桌面截图和输入执行的 Windows adapter；本次没有把 page viewport 截图假装成桌面截图。

有些 Chromium 自定义菜单不遵循系统菜单标志；页面焦点通常能帮助排除，但不能保证所有浏览器版本、菜单和窗口管理器都已覆盖。复杂多窗口和真实原生弹窗仍需部署环境端到端验证，必要时补 UI Automation 的控件级判断。

## 训练与验证

本轮回归结果：31 项测试通过，Python 编译检查通过；真实 Chrome 的脚本策略 smoke 仍完成 5 步任务，4 次 DOM 操作，定位模型调用 0 次。

继续使用统一 trajectory v2；scene 位于每一步 observation.info 和 prompt_metadata，policy input 仍保存实际输入。不新建 browser 专用训练格式。训练时可以分析 `0 -> 1 -> 0` 切换、AX 缺失降级、结构/视觉执行比例。

本次测试包括：标题含 Chrome 的普通进程不会误判、原生菜单/对话框、网页失焦、AX 抓取失败、捕获期间切换窗口、执行前切换窗口、旧 refs/源码失效、纯视觉输入不包含 desktop AX，以及同一任务中的 `桌面 -> 网页 -> 原生界面 -> 桌面`。混合任务的路由验证为 `visual -> browser_dom -> visual`，网页抓取/点击使用真实 Chrome，系统焦点切换信号为注入的测试数据。

Windows 前台 API 已做本机只读实测。测试没有实际操纵 Windows 多应用切换，也未依赖 Qwen API 或 OSWorld；这次验证的是状态检测、输入门控和执行路由，不是模型任务成功率。

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe script/smoke_browser_windows.py
```
