from __future__ import annotations

import argparse
import json
import multiprocessing
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description="雷电 / MuMu 无 ADB 控制工具")
    parser.add_argument("command", choices=["serve", "doctor", "list", "capture", "state"], nargs="?", default="doctor")
    parser.add_argument("instance_id", nargs="?")
    parser.add_argument("--state", choices=["start", "stop", "restart"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.command == "serve":
        from .server import create_server
        create_server(root).run(transport="stdio")
        return
    from .controller import Controller
    controller = Controller(root)
    try:
        if args.command == "doctor":
            from . import __version__
            from .win32 import is_elevated, is_restricted
            result = {"project": str(root), "python": sys.version.split()[0], "elevated": is_elevated(),
                      "tool_version": __version__, "query_restricted": is_restricted(),
                      "installations": {k:str(v) for k,v in controller.registry.roots.items()},
                      "instances": [controller.get_instance(i["instance_id"]) if i["instance_id"] else i
                                    for i in controller.list_instances()]}
        elif args.command == "list":
            result = controller.list_instances()
        elif args.command == "state":
            if not args.instance_id or not args.state:
                parser.error("state 需要 instance_id 和 --state")
            result = controller.set_instance_state(args.instance_id, args.state)
        else:
            if not args.instance_id:
                parser.error("capture 需要 instance_id")
            result = controller.screenshot(args.instance_id)
            output = args.output or root / "runtime" / (args.instance_id.replace(":", "-") + ".png")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(result.pop("image_bytes"))
            result["output"] = str(output.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
    finally:
        controller.close()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
