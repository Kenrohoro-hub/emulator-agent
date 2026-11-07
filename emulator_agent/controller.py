from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import threading
import time
import uuid

from PIL import Image

from .discovery import ControlError, Registry
from .workers import Worker


@dataclass
class Slot:
    lock: threading.RLock = field(default_factory=threading.RLock)
    worker: Worker | None = None
    frames: OrderedDict = field(default_factory=OrderedDict)
    cancel_epoch: int = 0
    start_lock: threading.Lock = field(default_factory=threading.Lock)


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ControlError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


class Controller:
    def __init__(self, project_root: Path):
        self.root = project_root
        self.registry = Registry(project_root / "config.local.json")
        self.slots: dict[str, Slot] = {}
        self.map_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.closing = threading.Event()

    def slot(self, instance_id: str) -> Slot:
        parts = instance_id.split(":") if isinstance(instance_id, str) else []
        if len(parts) != 2 or parts[0] not in ("ld", "mumu") or not parts[1].isdigit():
            raise ControlError("实例标识应为 ld:0 或 mumu:0")
        with self.map_lock:
            return self.slots.setdefault(instance_id, Slot())

    def list_instances(self):
        result = []
        for vendor in self.registry.roots:
            try:
                result.extend(i.public() for i in self.registry.list(vendor))
            except ControlError as exc:
                result.append({"vendor": vendor, "instance_id": None, "running": None,
                               "state": "query_failed", "error": str(exc)})
        return result

    def get_instance(self, instance_id):
        from . import __version__
        from .win32 import is_elevated
        instance = self.registry.get(instance_id)
        result = instance.public()
        result["tool_version"] = __version__
        result["capabilities"] = {"screenshot": True, "click": True, "long_press": True,
                                  "swipe": True, "press_key": True, "input_text": instance.vendor == "mumu"}
        result["supported_keys"] = (["home", "recent"] if instance.vendor == "ld" else
                                    ["back", "home", "recent", "enter", "volume_up", "volume_down"])
        if instance.running and instance.vendor == "ld":
            result["requires_elevation"] = is_elevated(instance.pid) and not is_elevated()
        result["business_success_requires_visual_verification"] = True
        return result

    def worker(self, slot: Slot, instance_id: str):
        if self.closing.is_set():
            raise ControlError("工具正在关闭，已停止接受新操作")
        current = self.registry.get(instance_id)
        if current.state != "running":
            if current.state == "stopped":
                raise ControlError("管理程序报告实例已停止；确认需要运行时再 set_instance_state(start)")
            raise ControlError(current.state_message)
        existing = slot.worker
        if existing and (not existing.alive or existing.instance.generation != current.generation
                         or (current.width and (existing.instance.width, existing.instance.height) != (current.width, current.height))):
            existing.close()
            slot.worker = None
            slot.frames.clear()
        if slot.worker is None:
            slot.worker = Worker(current)
        return slot.worker

    def log(self, instance_id, operation, status, elapsed):
        try:
            folder = self.root / "runtime/logs"
            folder.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            record = {"time": now.isoformat(), "instance": instance_id, "operation": operation,
                      "status": status, "elapsed_ms": round(elapsed * 1000)}
            with self.log_lock:
                with (folder / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            return "操作日志写入失败；操作结果仍有效，请勿因此重复输入或启停"

    def render_frame(self, slot, instance_id, result, crop=None, max_size=1600):
        if "png" not in result:
            return result
        full_size = (result["width"], result["height"])
        png = base64.b64decode(result.pop("png"))
        box = (0, 0, *full_size)
        if crop is not None:
            if len(crop) != 4:
                raise ControlError("crop 应为 [left, top, right, bottom]")
            left, top, right, bottom = crop
            integer(left, "left", 0, full_size[0]-1)
            integer(top, "top", 0, full_size[1]-1)
            integer(right, "right", left+1, full_size[0])
            integer(bottom, "bottom", top+1, full_size[1])
            box = tuple(crop)
        view_size = full_size
        if box != (0, 0, *full_size) or max(full_size) > max_size:
            with Image.open(BytesIO(png)) as source:
                image = source.crop(box)
                image.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
                data = BytesIO()
                image.save(data, "PNG")
                png = data.getvalue()
                view_size = image.size
        frame_id = uuid.uuid4().hex
        slot.frames[frame_id] = {"generation": slot.worker.instance.generation, "size": full_size,
                                 "box": box, "view_size": view_size}
        while len(slot.frames) > 16:
            slot.frames.popitem(last=False)
        result.update(instance_id=instance_id, frame_id=frame_id, width=view_size[0], height=view_size[1],
                      native_width=full_size[0], native_height=full_size[1], crop=list(box),
                      captured_at=datetime.now(timezone.utc).isoformat(), coordinate_space="returned_image",
                      image_bytes=png)
        return result

    def screenshot(self, instance_id, crop=None, max_size=1600):
        integer(max_size, "max_size", 128, 4096)
        slot = self.slot(instance_id)
        with slot.lock:
            worker = self.worker(slot, instance_id)
            start = time.monotonic()
            result = worker.request({"op": "screenshot"})
            warning = self.log(instance_id, "screenshot", result["status"], time.monotonic()-start)
            if warning:
                result.setdefault("warnings", []).append(warning)
            return self.render_frame(slot, instance_id, result, crop, max_size)

    def action(self, instance_id, op, *, frame_id=None, points=None, duration=.06, count=1, key=None, text=None,
               cancelled=None):
        if op not in ("click", "long_press", "swipe", "press_key", "input_text"):
            raise ControlError("不支持的动作")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not .01 <= duration <= 10:
            raise ControlError("duration 必须在 0.01 到 10 秒之间")
        integer(count, "count", 1, 2)
        if op == "press_key" and key not in ("back", "home", "recent", "enter", "volume_up", "volume_down"):
            raise ControlError("不支持的按键")
        if op == "input_text" and (not isinstance(text, str) or not 1 <= len(text) <= 4096 or "\0" in text):
            raise ControlError("文本长度应为 1 到 4096，且不得包含空字符")
        slot = self.slot(instance_id)
        epoch = slot.cancel_epoch
        is_cancelled = lambda: self.closing.is_set() or epoch != slot.cancel_epoch or (cancelled is not None and cancelled.is_set())
        with slot.lock:
            if is_cancelled():
                raise ControlError("排队中的操作已取消")
            worker = self.worker(slot, instance_id)
            request = {"op": op, "duration": duration, "count": count, "key": key, "text": text}
            if op in ("click", "long_press", "swipe"):
                frame = slot.frames.get(frame_id)
                if not frame or frame["generation"] != worker.instance.generation:
                    raise ControlError("截图编号无效或已过期，请先重新 screenshot")
                expected_points = 2 if op == "swipe" else 1
                if not isinstance(points, list) or len(points) != expected_points:
                    raise ControlError("触控坐标数量不正确")
                width, height = frame["view_size"]
                left, top, right, bottom = frame["box"]
                mapped = []
                for x, y in points:
                    integer(x, "x", 0, width-1)
                    integer(y, "y", 0, height-1)
                    mapped.append([left + x * (right-left) // width, top + y * (bottom-top) // height])
                request.update(points=mapped, expected_size=list(frame["size"]))
            slot.frames.clear()
            start = time.monotonic()
            try:
                result = worker.request(request, slot.start_lock, is_cancelled)
            except Exception:
                self.log(instance_id, op, "failed_or_unknown", time.monotonic()-start)
                raise
            warning = self.log(instance_id, op, result["status"], time.monotonic()-start)
            if warning:
                result.setdefault("warnings", []).append(warning)
            return self.render_frame(slot, instance_id, result)

    def set_instance_state(self, instance_id, state, *, cancelled=None):
        if state not in ("start", "stop", "restart"):
            raise ControlError("state 只能为 start、stop、restart")
        slot = self.slot(instance_id)
        _, epoch = self._cancel(slot)
        is_cancelled = lambda: self.closing.is_set() or epoch != slot.cancel_epoch or (cancelled is not None and cancelled.is_set())
        with slot.lock:
            if self.closing.is_set():
                raise ControlError("工具正在关闭，已停止接受实例启停")
            if is_cancelled():
                raise ControlError("排队中的启停请求已取消，未发送管理命令")
            instance = self.registry.get(instance_id)
            if slot.worker:
                slot.worker.close()
                slot.worker = None
            slot.frames.clear()
            start = time.monotonic()
            result = self.registry.lifecycle(instance, state, start_lock=slot.start_lock, is_cancelled=is_cancelled).public()
            warning = self.log(instance_id, state, "state_observed", time.monotonic()-start)
            if warning:
                result.setdefault("warnings", []).append(warning)
            return result

    def _cancel(self, slot):
        with slot.start_lock:
            slot.cancel_epoch += 1
            active = slot.worker.cancel() if slot.worker else False
            return active, slot.cancel_epoch

    def cancel(self, instance_id):
        slot = self.slot(instance_id)
        active, _ = self._cancel(slot)
        return {"instance_id": instance_id, "status": "cancel_requested", "was_active": active,
                "message": "持续触控将释放，排队输入和启停将取消；已发出的输入或管理命令不能撤销，重启可能停在关闭阶段，请查询实际状态"}

    def close(self):
        self.closing.set()
        with self.map_lock:
            slots = list(self.slots.values())
        for slot in slots:
            if slot.worker:
                slot.worker.cancel()
            with slot.lock:
                if slot.worker:
                    slot.worker.close()
                    slot.worker = None
