"""Isolated local PDF/Notepad fixture with independent task evidence."""
from __future__ import annotations

from dataclasses import replace
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
import uuid

from ..actions import Action
from ..adapters.windows_env import WindowsEnvironment
from .smoke_computer_windows import QuietHandler, open_notepad, test_pdf, window_title


EXPECTED_NOTE = "已下载 GUI Agent Browser Study 论文 PDF。跨应用任务验证完成。"


def crossed_applications(trajectory):
    """Require desktop -> foreground web page -> desktop in one trajectory."""
    flags = [step.get("observation", {}).get("info", {}).get("scene", {}).get("browser_use")
             for step in trajectory.get("steps", [])]
    phase = 0
    for flag in flags:
        if phase == 0 and flag == 0:
            phase = 1
        elif phase == 1 and flag == 1:
            phase = 2
        elif phase == 2 and flag == 0:
            return True
    return False


class ComputerFixture:
    def __init__(self, output, browser_config):
        self.output = Path(output).resolve()
        self.config = browser_config
        self.environment = self.server = self.worker = self.notepad = None

    def __enter__(self):
        self.directory = self.output / ("fixture-" + uuid.uuid4().hex[:12])
        self.directory.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "tests/fixtures/computer_papers.html"
        (self.directory / "index.html").write_bytes(source.read_bytes())
        self.pdf = test_pdf()
        (self.directory / "paper.pdf").write_bytes(self.pdf)
        self.downloads = self.directory / "downloads"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(self.directory)))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"
        self.notes = self.directory / ("agent-task-" + uuid.uuid4().hex[:8] + ".txt")
        self.task = (f"读取当前记事本任务。打开测试网页 {self.url}，检索 GUI Agent Browser Study，"
                     "下载论文 PDF。回到同一记事本文档 " + self.notes.stem +
                     "，把全文替换为下面这一行并保存，再核对下载与保存结果：\n" + EXPECTED_NOTE)
        self.notes.write_text(self.task, encoding="utf-8")
        # Acceptance never attaches a personal profile or overwrites existing downloads.
        isolated = replace(self.config, endpoint="", profile_dir="", download_dir=str(self.downloads))
        try:
            self.environment = WindowsEnvironment(browser_config=isolated, artifact_dir=self.output,
                                                  evaluator=self.evaluate)
            _, self.notepad = open_notepad(self.environment, self.notes)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def evaluate(self, environment):
        download = self.downloads / "agent-study.pdf"
        try:
            foreground = environment.native.probe()
            same_document = (self.notepad is not None and foreground.process_name == "notepad.exe"
                             and foreground.window_id == self.notepad.window_id
                             and self.notes.stem.lower() in window_title(environment.native.user, foreground.window_id).lower())
            return float(same_document and download.is_file() and download.read_bytes() == self.pdf
                         and self.notes.read_text(encoding="utf-8-sig").strip() == EXPECTED_NOTE)
        except (OSError, UnicodeError):
            return 0.0

    def __exit__(self, *_):
        if self.environment is not None:
            try:
                if (self.notepad is not None and
                        self.notes.stem.lower() in window_title(self.environment.native.user, self.notepad.window_id).lower()):
                    self.environment.native.activate(self.notepad.window_id)
                    self.environment.observe()
                    self.environment.step(Action("hotkey", {"keys": ["ctrl", "s"]}))
                    self.environment.observe()
                    self.environment.step(Action("hotkey", {"keys": ["ctrl", "w"]}))
            except Exception:
                pass
            finally:
                self.environment.close()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.worker is not None:
            self.worker.join(timeout=2)
