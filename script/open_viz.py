"""Open the local dashboard once its server is ready, then exit."""
import sys
import time
from urllib.request import build_opener, ProxyHandler
import webbrowser


def main():
    url = sys.argv[1]
    opener = build_opener(ProxyHandler({}))
    for _ in range(60):
        try:
            with opener.open(url + "/run/status", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    return
        except OSError:
            pass
        time.sleep(0.5)


if __name__ == "__main__":
    main()
