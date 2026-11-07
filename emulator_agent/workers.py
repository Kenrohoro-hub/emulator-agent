from __future__ import annotations

from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading

from .discovery import ControlError, Instance

# Covers base64 PNG of the native backend's maximum 24 million RGB pixels.
MAX_RESPONSE_BYTES = 128 * 1024 * 1024

class Worker:
    def __init__(self, instance: Instance):
        self.instance = instance
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.active = threading.Event()
        self.responses = queue.Queue()
        self.process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "emulator_agent.worker_entry"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self.read_responses, daemon=True)
        self.reader.start()
        try:
            self.send({"instance": asdict(instance)})
            response = self.responses.get(timeout=15)
            if not response.get("ready"):
                raise ControlError(response.get("error", "原生工作进程未就绪"))
        except (queue.Empty, OSError) as exc:
            self.close()
            raise ControlError("原生工作进程启动失败或超时") from exc
        except ControlError:
            self.close()
            raise

    @property
    def alive(self):
        return self.process.poll() is None

    def send(self, value):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        with self.write_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def read_responses(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_RESPONSE_BYTES)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise ValueError("原生响应超出大小上限")
                self.responses.put(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            self.responses.put({"ok": False, "disconnected": True,
                                "error": "原生工作进程断开或响应无效，结果未知；请先重新截图"})

    def request(self, request: dict, start_lock=None, is_cancelled=None) -> dict:
        with self.lock:
            if not self.alive:
                raise ControlError("原生工作进程已退出；请重新截图后再操作")
            try:
                with start_lock if start_lock is not None else nullcontext():
                    if is_cancelled is not None and is_cancelled():
                        raise ControlError("排队中的操作已取消")
                    self.active.set()
                    self.send(request)
                try:
                    response = self.responses.get(timeout=15)
                except queue.Empty as exc:
                    self.cancel()
                    self.close()
                    raise ControlError("原生接口超时，结果未知；自有工作进程已回收，请先截图，勿重复输入") from exc
                if not response["ok"]:
                    if response.get("disconnected"):
                        self.close()
                    raise ControlError(response["error"])
                return response["result"]
            except (BrokenPipeError, OSError) as exc:
                self.close()
                raise ControlError("原生接口断开，结果未知；请先重新截图") from exc
            finally:
                self.active.clear()

    def cancel(self):
        if self.alive:
            try:
                self.send({"op": "cancel"})
            except OSError:
                pass
        return self.active.is_set()

    def close(self):
        if self.alive:
            try:
                self.send({"op": "close"})
                self.process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                # Only this Popen's child is terminated, never a simulator process.
                self.process.terminate()
                self.process.wait(timeout=5)
        self.process.stdin.close()
        self.reader.join(timeout=1)
        self.process.stdout.close()
