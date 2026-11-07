from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import venv


def main():
    if os.name != "nt" or sys.version_info < (3,11) or struct.calcsize("P") != 8:
        raise RuntimeError("需要 Windows 和 64 位 Python 3.11 或更新版本")
    root = Path(__file__).resolve().parent
    python = root / ".venv/Scripts/python.exe"
    if not python.is_file():
        print("正在准备独立运行环境……",file=sys.stderr)
        venv.create(root / ".venv", with_pip=True)
    check = "import importlib.metadata as m; a=m.version('mcp').split('.'); b=m.version('Pillow').split('.'); assert int(a[0])==1 and int(a[1])>=30 and int(b[0])==12"
    result = subprocess.run([str(python), "-c", check], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        if subprocess.run([str(python), "-c", "import pip"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode:
            subprocess.run([str(python), "-m", "ensurepip", "--upgrade"], check=True,
                           stdout=sys.stderr, stderr=sys.stderr)
        print("正在安装必要的 MCP 和图片处理依赖……",file=sys.stderr)
        subprocess.run([str(python), "-m", "pip", "install", "mcp>=1.30,<2", "Pillow>=12,<13"],
                       check=True, stdout=sys.stderr, stderr=sys.stderr)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    config = {"mcpServers": {"emulator-agent": {"command": str(python),
              "args": ["-X", "utf8", str(root / "main.py"), "serve"]}}}
    (runtime / "mcp-config.json").write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding="utf-8")
    print("运行环境已就绪。接入配置位于 runtime/mcp-config.json",file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"准备失败：{exc}",file=sys.stderr)
        raise SystemExit(1)
