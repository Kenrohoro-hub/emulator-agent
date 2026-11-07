from __future__ import annotations

import ctypes as C
from io import BytesIO
from pathlib import Path
import time

from PIL import Image

from .discovery import ControlError, Instance
from .win32 import is_elevated, post, signature, user, validate_window


def check_size(width: int, height: int):
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 24_000_000):
        raise ControlError("模拟器返回的画面尺寸无效或过大")


class Native:
    def __init__(self, instance: Instance):
        self.instance = instance
        self.width, self.height = 0, 0
        self.touch_active = False

    def screenshot(self) -> Image.Image:
        raise NotImplementedError

    def touch(self, x: int, y: int):
        raise NotImplementedError

    def release(self):
        raise NotImplementedError

    def close(self):
        self.release()

    def execute(self, request: dict, cancelled) -> dict:
        op = request["op"]
        before = self.screenshot()
        if op == "screenshot":
            return self.frame_result(before)
        if cancelled.is_set():
            raise ControlError("操作已取消，未发送输入")
        expected = request.get("expected_size")
        if expected and list(before.size) != expected:
            raise ControlError("画面尺寸已改变，请重新截图后再输入")
        if op == "press_key":
            self.key(request["key"])
        elif op == "input_text":
            self.text(request["text"])
        elif op in ("click", "long_press", "swipe"):
            points = request["points"]
            for x, y in points:
                if not (isinstance(x, int) and isinstance(y, int) and 0 <= x < self.width and 0 <= y < self.height):
                    raise ControlError("坐标超出当前画面，请重新截图")
            try:
                if cancelled.is_set():
                    raise ControlError("操作已取消")
                duration = request["duration"]
                if op == "swipe":
                    x1, y1 = points[0]
                    x2, y2 = points[1]
                    steps = max(2, round(duration / .016))
                    for i in range(steps + 1):
                        if cancelled.is_set():
                            raise ControlError("操作已取消，触控已释放")
                        self.touch(round(x1 + (x2-x1)*i/steps), round(y1 + (y2-y1)*i/steps))
                        if i < steps:
                            cancelled.wait(duration / steps)
                else:
                    for i in range(request.get("count", 1)):
                        if cancelled.is_set():
                            raise ControlError("操作已取消")
                        self.touch(*points[0])
                        cancelled.wait(duration)
                        self.release()
                        if i == 0 and request.get("count", 1) == 2:
                            cancelled.wait(.1)
                if cancelled.is_set():
                    raise ControlError("操作已取消，触控已释放")
            finally:
                self.release()
        else:
            raise ControlError("不支持的操作")
        cancelled.wait(.25)
        try:
            result = self.frame_result(self.screenshot())
            result["status"] = "input_sent"
            result["task_verified"] = False
            return result
        except Exception as exc:
            return {"status": "input_sent", "task_verified": False,
                    "screenshot_error": str(exc), "message": "输入已发送，但新截图失败；请勿直接重复输入"}

    def frame_result(self, image: Image.Image) -> dict:
        output = BytesIO()
        image.save(output, format="PNG")
        import base64
        return {"status": "captured", "width": image.width, "height": image.height,
                "png": base64.b64encode(output.getvalue()).decode("ascii")}

    def key(self, key: str):
        raise ControlError("此后端不支持该按键")

    def text(self, text: str):
        raise ControlError("此后端尚未验证无 ADB 文本输入，请勿自动切换其他输入通道")


class LDNative(Native):
    def __init__(self, instance: Instance):
        super().__init__(instance)
        self.lib = C.WinDLL(str(Path(instance.root) / "ldopengl64.dll"))
        create = signature(self.lib, "CreateScreenShotInstance", C.c_void_p, C.c_uint, C.c_uint)
        self.obj = create(instance.index, instance.pid)
        if not self.obj:
            raise ControlError("雷电截图实例创建失败")
        table = C.cast(self.obj, C.POINTER(C.POINTER(C.c_void_p))).contents
        self.cap = C.WINFUNCTYPE(C.c_void_p, C.c_void_p)(table[1])
        self.free = C.WINFUNCTYPE(None, C.c_void_p)(table[2])
        self.width, self.height = instance.width, instance.height
        check_size(self.width, self.height)

    def screenshot(self) -> Image.Image:
        validate_window(self.instance.render_hwnd, self.instance.pid, "RenderWindow")
        for _ in range(20):
            ptr = self.cap(self.obj)
            if ptr:
                return Image.frombytes("RGB", (self.width, self.height),
                                       C.string_at(ptr, self.width*self.height*3), "raw", "BGR", 0, -1)
            time.sleep(.05)
        raise ControlError("雷电原生截图未返回画面；请检查实例是否启动完成")

    def touch(self, x: int, y: int):
        inst = self.instance
        if is_elevated(inst.pid) and not is_elevated():
            raise ControlError("雷电以管理员运行：请以普通权限关闭再启动该实例，或由管理员权限的调用端运行工具")
        if user.IsIconic(inst.hwnd):
            raise ControlError("雷电最小化输入尚未验证，请先还原窗口；遮挡窗口可继续使用")
        width, height = validate_window(inst.render_hwnd, inst.pid, "RenderWindow")
        if not self.touch_active:
            self.client_size = (width, height)
        if self.client_size != (width, height):
            raise ControlError("窗口在触控期间改变尺寸，已取消")
        cx = min(width-1, x * width // self.width)
        cy = min(height-1, y * height // self.height)
        self.last_position = cx | (cy << 16)
        post(inst.render_hwnd, 0x200, 1 if self.touch_active else 0, self.last_position)
        if not self.touch_active:
            post(inst.render_hwnd, 0x201, 1, self.last_position)
            self.touch_active = True

    def release(self):
        if self.touch_active:
            try:
                validate_window(self.instance.render_hwnd, self.instance.pid, "RenderWindow")
                post(self.instance.render_hwnd, 0x202, 0, self.last_position)
            finally:
                self.touch_active = False

    def key(self, key: str):
        values = {"back": 0x1B, "home": 0x70, "recent": 0x71, "enter": 0x0D}
        if key not in values:
            raise ControlError("不支持的按键")
        inst = self.instance
        validate_window(inst.hwnd, inst.pid, "LDPlayerMainFrame")
        code = values[key]
        post(inst.hwnd, 0x100, code, 1)
        try:
            time.sleep(.05)
        finally:
            post(inst.hwnd, 0x101, code, 0xC0000001)

    def close(self):
        try:
            self.release()
        finally:
            if self.obj:
                self.free(self.obj)
                self.obj = None


class MuMuNative(Native):
    def __init__(self, instance: Instance):
        super().__init__(instance)
        root = Path(instance.root)
        candidates = [root / "nx_device/12.0/shell/sdk/external_renderer_ipc.dll",
                      root / "nx_main/sdk/external_renderer_ipc.dll",
                      root / "shell/sdk/external_renderer_ipc.dll"]
        dll = next((p for p in candidates if p.is_file()), None)
        if dll is None:
            raise ControlError("未找到 MuMu 截图和触控 DLL")
        self.lib = C.CDLL(str(dll))
        signature(self.lib, "nemu_connect", C.c_int, C.c_wchar_p, C.c_int)
        signature(self.lib, "nemu_disconnect", None, C.c_int)
        signature(self.lib, "nemu_capture_display", C.c_int, C.c_int, C.c_uint, C.c_int,
                  C.POINTER(C.c_int), C.POINTER(C.c_int), C.c_void_p)
        signature(self.lib, "nemu_input_event_touch_down", C.c_int, C.c_int, C.c_int, C.c_int, C.c_int)
        signature(self.lib, "nemu_input_event_touch_up", C.c_int, C.c_int, C.c_int)
        signature(self.lib, "nemu_input_event_key_down", C.c_int, C.c_int, C.c_int, C.c_int)
        signature(self.lib, "nemu_input_event_key_up", C.c_int, C.c_int, C.c_int, C.c_int)
        signature(self.lib, "nemu_input_text", C.c_int, C.c_int, C.c_int, C.c_char_p)
        self.handle = self.lib.nemu_connect(str(root), instance.index)
        if self.handle <= 0:
            raise ControlError("MuMu 原生接口连接失败")

    def checked(self, name: str, *args):
        result = getattr(self.lib, name)(self.handle, *args)
        if result != 0:
            raise ControlError(f"MuMu {name} 失败：{result}；操作结果可能未知，请先截图")

    def screenshot(self) -> Image.Image:
        width, height = C.c_int(), C.c_int()
        self.checked("nemu_capture_display", 0, 0, C.byref(width), C.byref(height), None)
        check_size(width.value, height.value)
        self.width, self.height = width.value, height.value
        buffer = (C.c_ubyte * (self.width*self.height*4))()
        self.checked("nemu_capture_display", 0, len(buffer), C.byref(width), C.byref(height), buffer)
        if (width.value, height.value) != (self.width, self.height):
            raise ControlError("截图期间画面尺寸变化，请重试截图")
        return Image.frombytes("RGBA", (self.width, self.height), bytes(buffer)).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM).convert("RGB")

    def touch(self, x: int, y: int):
        self.checked("nemu_input_event_touch_down", 0, x, y)
        self.touch_active = True

    def release(self):
        if self.touch_active:
            try:
                self.checked("nemu_input_event_touch_up", 0)
            finally:
                self.touch_active = False

    def key(self, key: str):
        values = {"back": 158, "home": 172, "recent": 580, "enter": 28,
                  "volume_up": 115, "volume_down": 114}
        if key not in values:
            raise ControlError("不支持的按键")
        code = values[key]
        self.checked("nemu_input_event_key_down", 0, code)
        try:
            time.sleep(.05)
        finally:
            self.checked("nemu_input_event_key_up", 0, code)

    def text(self, text: str):
        value = text.encode("utf-8")
        self.checked("nemu_input_text", len(value), value)

    def close(self):
        try:
            self.release()
        finally:
            if self.handle:
                self.lib.nemu_disconnect(self.handle)
                self.handle = 0
