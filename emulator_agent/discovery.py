from __future__ import annotations

import csv
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


def run_command(executable: Path, *args: str, timeout: float = 15) -> str:
    try:
        result = subprocess.run(
            [str(executable), *args], capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise ControlError("模拟器管理命令超时；请查询实际状态，不要直接重复操作") from exc
    except OSError as exc:
        raise ControlError(f"无法运行模拟器管理程序：{exc}") from exc
    if result.returncode:
        raise ControlError(f"模拟器管理命令失败（退出码 {result.returncode}）")
    return decode(result.stdout).strip()


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
    decoder = json.JSONDecoder()
    records = []
    while text.strip():
        try:
            obj, offset = decoder.raw_decode(text.lstrip())
        except json.JSONDecodeError as exc:
            raise ControlError("MuMu 实例查询未返回有效 JSON") from exc
        records.extend(obj if isinstance(obj, list) else [obj])
        text = text.lstrip()[offset:]
    if len(records) == 1 and "index" not in records[0]:
        records = list(records[0].values())
    result = []
    for obj in records:
        if not isinstance(obj, dict) or "index" not in obj:
            raise ControlError("不支持的 MuMu 实例格式")
        if obj.get("error_code", 0):
            raise ControlError(f"MuMu 实例查询失败：{obj['error_code']}")
        def hwnd(key):
            value = obj.get(key, 0)
            return int(value, 16) if isinstance(value, str) else int(value)
        result.append(Instance("mumu", int(obj["index"]), str(obj.get("name", "MuMu")),
                               str(root), bool(obj.get("is_android_started")),
                               int(obj.get("pid", 0)), hwnd("main_wnd"), hwnd("render_wnd")))
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
        self.roots = discover_roots(config_file)

    def list(self) -> list[Instance]:
        instances = []
        for vendor, root in self.roots.items():
            if vendor == "ld":
                instances.extend(parse_ld(run_command(root / "ldconsole.exe", "list2"), root))
            else:
                instances.extend(parse_mumu(run_command(root / "nx_main/MuMuManager.exe", "info", "--vmindex", "all"), root))
        return instances

    def get(self, instance_id: str) -> Instance:
        if not isinstance(instance_id, str) or not instance_id.split(":")[-1].isdigit():
            raise ControlError("实例标识应为 ld:0 或 mumu:0")
        for instance in self.list():
            if instance.id == instance_id:
                return instance
        raise ControlError(f"未找到实例 {instance_id}")

    def lifecycle(self, instance: Instance, state: str):
        import time
        if state not in ("start", "stop", "restart"):
            raise ControlError("state 只能为 start、stop、restart")
        if state == "restart":
            self.lifecycle(instance, "stop")
            return self.lifecycle(self.get(instance.id), "start")
        if instance.running == (state == "start"):
            return instance
        root = Path(instance.root)
        if instance.vendor == "ld":
            run_command(root / "ldconsole.exe", "launch" if state == "start" else "quit", "--index", str(instance.index))
        else:
            output = run_command(root / "nx_main/MuMuManager.exe", "control", "--vmindex", str(instance.index),
                                 "launch" if state == "start" else "shutdown")
            if output:
                reply = json.loads(output)
                if reply.get("errcode", 0):
                    raise ControlError(f"MuMu 管理操作失败：{reply['errcode']}")
        deadline = time.monotonic() + (90 if state == "start" else 40)
        while time.monotonic() < deadline:
            current = self.get(instance.id)
            if current.running == (state == "start") and (state == "stop" or current.render_hwnd):
                return current
            time.sleep(.5)
        raise ControlError("实例状态等待超时；请先查询状态，避免重复启停")
