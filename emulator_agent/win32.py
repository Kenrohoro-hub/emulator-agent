from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W

from .discovery import ControlError

user = C.WinDLL("user32", use_last_error=True)
kernel = C.WinDLL("kernel32", use_last_error=True)
advapi = C.WinDLL("advapi32", use_last_error=True)


def signature(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


signature(user, "SetProcessDpiAwarenessContext", W.BOOL, W.HANDLE)(W.HANDLE(-4))
signature(user, "GetClientRect", W.BOOL, W.HWND, C.POINTER(W.RECT))
signature(user, "GetWindowThreadProcessId", W.DWORD, W.HWND, C.POINTER(W.DWORD))
signature(user, "GetClassNameW", C.c_int, W.HWND, W.LPWSTR, C.c_int)
signature(user, "PostMessageW", W.BOOL, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
signature(user, "MapVirtualKeyW", W.UINT, W.UINT, W.UINT)
signature(user, "IsWindow", W.BOOL, W.HWND)
signature(user, "IsIconic", W.BOOL, W.HWND)
signature(user, "GetForegroundWindow", W.HWND)
signature(user, "GetCursorPos", W.BOOL, C.POINTER(W.POINT))
signature(kernel, "OpenProcess", W.HANDLE, W.DWORD, W.BOOL, W.DWORD)
signature(kernel, "GetCurrentProcess", W.HANDLE)
signature(kernel, "CloseHandle", W.BOOL, W.HANDLE)
signature(advapi, "OpenProcessToken", W.BOOL, W.HANDLE, W.DWORD, C.POINTER(W.HANDLE))
signature(advapi, "GetTokenInformation", W.BOOL, W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD))


def is_elevated(pid: int = 0) -> bool:
    process = kernel.OpenProcess(0x1000, False, pid) if pid else kernel.GetCurrentProcess()
    if not process:
        raise ControlError("无法查询目标进程权限")
    token, size, elevated = W.HANDLE(), W.DWORD(), W.DWORD()
    try:
        if not advapi.OpenProcessToken(process, 8, C.byref(token)):
            raise ControlError("无法读取进程权限")
        if not advapi.GetTokenInformation(token, 20, C.byref(elevated), 4, C.byref(size)):
            raise ControlError("无法读取进程权限级别")
        return bool(elevated.value)
    finally:
        if token:
            kernel.CloseHandle(token)
        if pid:
            kernel.CloseHandle(process)


def validate_window(hwnd: int, pid: int, expected_class: str | None = None) -> tuple[int, int]:
    actual = W.DWORD()
    if not user.IsWindow(hwnd):
        raise ControlError("目标窗口已失效，请重新截图")
    user.GetWindowThreadProcessId(hwnd, C.byref(actual))
    if actual.value != pid:
        raise ControlError("窗口归属改变，已停止操作")
    if expected_class:
        name = C.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, 256)
        if name.value != expected_class:
            raise ControlError("渲染窗口类型不匹配，已停止操作")
    rect = W.RECT()
    if not user.GetClientRect(hwnd, C.byref(rect)) or min(rect.right, rect.bottom) <= 0:
        raise ControlError("目标窗口没有有效客户区")
    return rect.right, rect.bottom


def post(hwnd: int, message: int, wparam: int = 0, lparam: int = 0):
    C.set_last_error(0)
    if not user.PostMessageW(hwnd, message, wparam, lparam):
        code = C.get_last_error()
        if code == 5:
            raise ControlError("Windows 拒绝后台输入：请以普通权限重启雷电，或由相同权限的调用端运行工具")
        raise ControlError(f"窗口消息发送失败：{code}")


def desktop_context() -> dict:
    point = W.POINT()
    user.GetCursorPos(C.byref(point))
    return {"foreground": user.GetForegroundWindow(), "cursor": [point.x, point.y]}
