"""Observe a real Windows Chrome fixture without OSWorld or API credentials.

For a Qwen-driven task use run_browser_windows.py.
For the deterministic pipeline smoke use script/smoke_browser_windows.py.
"""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from osworld_agent.adapters.browser_env import BrowserEnvironment


def main():
    url = (Path(__file__).parent / "tests" / "fixtures" / "browser_task.html").resolve().as_uri()
    env = BrowserEnvironment(url)
    try:
        obs = env.reset("Inspect browser")
        block = obs.context[0]
        print(json.dumps({"url": block["url"], "title": block["title"], "nodes": len(block["nodes"]),
                          "capture_ms": block["capture_ms"]}, ensure_ascii=False, indent=2))
        for node in block["nodes"]:
            if node["direct"]:
                print(node["ref"], node["role"], node["name"])
        print("Sanitized DOM characters:", len(env.get_page_source()))
    finally:
        env.close()


if __name__ == "__main__":
    main()
