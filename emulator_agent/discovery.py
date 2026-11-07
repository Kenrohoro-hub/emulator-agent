from __future__ import annotations

import csv
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
from dataclasses import asdict, dataclass


class ControlError(RuntimeError):
    pass


def decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ControlError("模拟器命令输出编码无法识别")


def run_command(executable: Path, *args: str, timeout: float = 15,
                start_lock=None, is_cancelled=None) -> str:
    try:
        with start_lock if start_lock is not None else nullcontext():
            if is_cancelled is not None and is_cancelled():
                raise ControlError("启停请求已取消，未发送后续管理命令；已发出的命令不能撤销，请查询实例状态")
            process = subprocess.Popen(
                [str(executable), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        with process:
            try:
                output, _ = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Reap only the management command created above, never the emulator.
                process.kill()
                process.communicate()
                raise
    except subprocess.TimeoutExpired as exc:
        raise ControlError("模拟器管理命令超时；请查询实际状态，不要直接重复操作") from exc
    except OSError as exc:
        raise ControlError(f"无法运行模拟器管理程序：{exc}") from exc
    if process.returncode:
        raise ControlError(f"模拟器管理命令失败（退出码 {process.returncode}）")
    return decode(output).strip()


@dataclass
class Instance:
    vendor: str
    index: int
    name: str
    root: str
    running: bool
    pid: int = 0
    hwnd: int = 0
    render_hwnd: int = 0
    width: int = 0
    height: int = 0
    query_restricted: bool = False

    @property
    def state(self) -> str:
        if self.running:
            return "running" if self.pid and self.render_hwnd else "starting"
        if self.query_restricted or self.pid or self.hwnd or self.render_hwnd:
            return "unknown"
        return "stopped"

    @property
    def state_message(self) -> str:
        return {
            "running": "管理程序报告 Android 正在运行；实际画面以截图为准",
            "starting": "Android 正在启动或渲染窗口未就绪，请稍后查询或截图，不要重复启动",
            "unknown": "无法确认运行状态：可能正在启停，或查询受沙箱/桌面会话限制；请检查调用环境，不要重复启停",
            "stopped": "管理程序报告实例已停止，且未返回进程或窗口",
        }[self.state]

    @property
    def id(self) -> str:
        return f"{self.vendor}:{self.index}"

    @property
    def generation(self) -> tuple:
        return self.pid, self.hwnd, self.render_hwnd

    def public(self) -> dict:
        result = asdict(self)
        result.pop("root")
        result["instance_id"] = self.id
        result.update(state=self.state, state_message=self.state_message)
        return result


def parse_ld(text: str, root: Path) -> list[Instance]:
    instances = []
    for row in csv.reader(text.splitlines()):
        if not row:
            continue
        if len(row) not in (7, 10):
            raise ControlError("雷电 list2 格式不支持，未继续猜测实例")
        try:
            index, hwnd, render, running, pid = map(int, (row[0], *row[2:6]))
            width, height = map(int, row[7:9]) if len(row) == 10 else (0, 0)
        except ValueError as exc:
            raise ControlError("雷电实例信息包含无效数字") from exc
        instances.append(Instance("ld", index, row[1], str(root), bool(running),
                                  max(pid, 0), hwnd, render, width, height))
    return instances


def parse_mumu(text: str, root: Path) -> list[Instance]:
    def number(value, base=10):
        if type(value) is not int and not isinstance(value, str):
            raise ValueError("Expected an integer or integer string")
        parsed = int(value, base) if isinstance(value, str) else value
        if parsed < 0:
            raise ValueError("Expected a non-negative integer")
        return parsed

    decoder = json.JSONDecoder()
    records = []
    while text.strip():
        try:
            obj, offset = decoder.raw_decode(text.lstrip())
        except json.JSONDecodeError as exc:
            raise ControlError("MuMu 实例查询未返回有效 JSON") from exc
        if not isinstance(obj, (dict, list)):
            raise ControlError("不支持的 MuMu 实例格式")
        records.extend(obj if isinstance(obj, list) else [obj])
        text = text.lstrip()[offset:]
    if len(records) == 1 and isinstance(records[0], dict) and "index" not in records[0]:
        if records[0].get("error_code", 0):
            raise ControlError(f"MuMu 实例查询失败：{records[0]['error_code']}")
        records = list(records[0].values())
    result = []
    for obj in records:
        if not isinstance(obj, dict) or "index" not in obj:
            raise ControlError("不支持的 MuMu 实例格式")
        if obj.get("error_code", 0):
            raise ControlError(f"MuMu 实例查询失败：{obj['error_code']}")
        running = obj.get("is_android_started", False)
        if not isinstance(running, bool):
            raise ControlError("MuMu 实例信息包含无效运行标记")
        try:
            result.append(Instance("mumu", number(obj["index"]), str(obj.get("name", "MuMu")),
                                   str(root), running, number(obj.get("pid", 0)),
                                   number(obj.get("main_wnd", 0), 16), number(obj.get("render_wnd", 0), 16)))
        except (ValueError, TypeError, OverflowError) as exc:
            raise ControlError("MuMu 实例信息包含无效数字") from exc
    return result


def discover_roots(config_file: Path | None = None) -> dict[str, Path]:
    if os.name != "nt":
        raise ControlError("此工具仅支持 Windows")
    import winreg
    overrides = {}
    if config_file and config_file.is_file():
        data = json.loads(config_file.read_text(encoding="utf-8"))
        for vendor in ("ld", "mumu"):
            if data.get(vendor):
                path = Path(data[vendor]).expanduser().resolve()
                executable = path / ("ldconsole.exe" if vendor == "ld" else "nx_main/MuMuManager.exe")
                if not executable.is_file():
                    raise ControlError(f"配置的 {vendor} 安装路径缺少管理程序")
                overrides[vendor] = path
    roots = dict(overrides)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for branch in (r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                       r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                with winreg.OpenKey(hive, branch) as parent:
                    count = winreg.QueryInfoKey(parent)[0]
                    for i in range(count):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as key:
                                name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                                if not any(x in name.lower() for x in ("雷电", "ldplayer", "mumu")):
                                    continue
                                vendor = "mumu" if "mumu" in name.lower() else "ld"
                                if vendor in overrides:
                                    continue
                                try:
                                    raw = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                                except OSError:
                                    raw = ""
                                if not raw:
                                    icon = str(winreg.QueryValueEx(key, "DisplayIcon")[0]).split(",")[0].strip('"')
                                    raw = str(Path(icon).parent)
                                path = Path(os.path.expandvars(raw)).resolve()
                                if vendor == "mumu" and path.name.lower() in ("nx_main", "shell"):
                                    path = path.parent
                                expected = path / ("ldconsole.exe" if vendor == "ld" else "nx_main/MuMuManager.exe")
                                if expected.is_file():
                                    if vendor in roots and roots[vendor] != path:
                                        raise ControlError(f"发现多个 {vendor} 安装，请在 config.local.json 指定路径")
                                    roots[vendor] = path
                        except OSError:
                            continue
            except OSError:
                continue
    return roots


class Registry:
    def __init__(self, config_file: Path | None = None):
        from .win32 import is_restricted
        self.roots = discover_roots(config_file)
        self.query_restricted = is_restricted()

    def list(self, vendor: str) -> list[Instance]:
        if vendor not in self.roots:
            raise ControlError(f"未发现 {vendor} 安装，请检查安装记录或 config.local.json")
        root = self.roots[vendor]
        if vendor == "ld":
            instances = parse_ld(run_command(root / "ldconsole.exe", "list2"), root)
        else:
            instances = parse_mumu(run_command(root / "nx_main/MuMuManager.exe", "info", "--vmindex", "all"), root)
        for instance in instances:
            instance.query_restricted = self.query_restricted
        return instances

    def get(self, instance_id: str) -> Instance:
        parts = instance_id.split(":") if isinstance(instance_id, str) else []
        if len(parts) != 2 or parts[0] not in ("ld", "mumu") or not parts[1].isdigit():
            raise ControlError("实例标识应为 ld:0 或 mumu:0")
        for instance in self.list(parts[0]):
            if instance.id == instance_id:
                return instance
        raise ControlError(f"未找到实例 {instance_id}")

    def lifecycle(self, instance: Instance, state: str, *, start_lock=None, is_cancelled=None):
        import time
        def check_cancelled():
            if is_cancelled is not None and is_cancelled():
                raise ControlError("启停请求已取消；已发出的命令不能撤销，重启可能停在关闭阶段，请查询实例状态")

        if state not in ("start", "stop", "restart"):
            raise ControlError("state 只能为 start、stop、restart")
        check_cancelled()
        if instance.state == "unknown" or (state == "start" and instance.state == "starting"):
            raise ControlError(instance.state_message)
        if state == "restart":
            self.lifecycle(instance, "stop", start_lock=start_lock, is_cancelled=is_cancelled)
            check_cancelled()
            return self.lifecycle(self.get(instance.id), "start", start_lock=start_lock, is_cancelled=is_cancelled)
        if instance.state == ("running" if state == "start" else "stopped"):
            return instance
        root = Path(instance.root)
        if instance.vendor == "ld":
            run_command(root / "ldconsole.exe", "launch" if state == "start" else "quit", "--index", str(instance.index),
                        start_lock=start_lock, is_cancelled=is_cancelled)
        else:
            output = run_command(root / "nx_main/MuMuManager.exe", "control", "--vmindex", str(instance.index),
                                 "launch" if state == "start" else "shutdown", start_lock=start_lock, is_cancelled=is_cancelled)
            if output:
                reply = json.loads(output)
                if reply.get("errcode", 0):
                    raise ControlError(f"MuMu 管理操作失败：{reply['errcode']}")
        deadline = time.monotonic() + (90 if state == "start" else 40)
        while time.monotonic() < deadline:
            check_cancelled()
            current = self.get(instance.id)
            check_cancelled()
            if current.state == ("running" if state == "start" else "stopped"):
                return current
            time.sleep(.5)
        raise ControlError("实例状态等待超时；请先查询状态，避免重复启停")
