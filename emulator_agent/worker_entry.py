from __future__ import annotations

import json
import os
import queue
import sys
import threading


def main():
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    # Preserve a private protocol handle before vendor DLLs can print on stdout.
    with open(os.devnull, "wb") as sink:
        os.dup2(sink.fileno(), sys.stdout.fileno())
    native = None
    cancelled = threading.Event()
    requests = queue.Queue()

    def reply(value):
        output.write(json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n")

    def read_requests():
        try:
            for line in sys.stdin.buffer:
                request = json.loads(line)
                op = request.get("op")
                if op in ("cancel", "close"):
                    cancelled.set()
                    if op == "close":
                        break
                else:
                    cancelled.clear()
                    requests.put(request)
        finally:
            cancelled.set()
            requests.put(None)

    try:
        from .discovery import Instance
        from .native import LDNative, MuMuNative
        initial = json.loads(sys.stdin.buffer.readline())
        instance = Instance(**initial["instance"])
        native = (LDNative if instance.vendor == "ld" else MuMuNative)(instance)
        reader = threading.Thread(target=read_requests, daemon=True)
        reader.start()
        reply({"ok": True, "ready": True})
        while (request := requests.get()) is not None:
            try:
                reply({"ok": True, "result": native.execute(request, cancelled)})
            except Exception as exc:
                reply({"ok": False, "error": str(exc)})
    except Exception as exc:
        try:
            reply({"ok": False, "error": str(exc)})
        except OSError:
            pass
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            output.close()


if __name__ == "__main__":
    main()
