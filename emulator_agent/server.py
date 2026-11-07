from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations

from .controller import Controller
from .discovery import ControlError


def create_server(root: Path):
    controller = Controller(root)

    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            await asyncio.to_thread(controller.close)

    server = FastMCP("emulator-agent", lifespan=lifespan, instructions=(
        "控制本机雷电与 MuMu，全程不使用 ADB。先 list_instances 再 screenshot。"
        "点击坐标必须引用截图返回的 frame_id，坐标以返回图片为准。每次操作后使用新 frame_id。"
        "input_sent 仅表示输入已发出，不表示业务成功，请根据返回画面判断。"
        "超时或结果未知时，先截图，禁止盲目重复点击。"))

    async def invoke(fn, *args, **kwargs):
        try:
            result = await asyncio.to_thread(fn, *args, **kwargs)
        except asyncio.CancelledError:
            if fn == controller.action and args:
                controller.cancel(args[0])
            raise
        if isinstance(result, dict) and "image_bytes" in result:
            import base64
            data = result.pop("image_bytes")
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False)),
                    ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")]
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

    @server.tool(annotations=read, structured_output=False)
    async def list_instances():
        """列出雷电、MuMu 实例和运行状态，不会启动实例。"""
        return await invoke(controller.list_instances)

    @server.tool(annotations=read, structured_output=False)
    async def get_instance(instance_id: str):
        """获取指定实例状态、可用能力和权限提示。"""
        return await invoke(controller.get_instance, instance_id)

    @server.tool(annotations=ToolAnnotations(destructiveHint=True), structured_output=False)
    async def set_instance_state(instance_id: str, state: str):
        """启动(start)、关闭(stop)或重启(restart)一个明确指定的实例。关闭会中断该实例应用。"""
        return await invoke(controller.set_instance_state, instance_id, state)

    @server.tool(annotations=read, structured_output=False)
    async def screenshot(instance_id: str, crop: list[int] | None = None, max_size: int = 1600):
        """返回实际 PNG 图片及 frame_id。crop 为原始画面的 [left,top,right,bottom]；后续坐标按返回图片计算。"""
        return await invoke(controller.screenshot, instance_id, crop, max_size)

    @server.tool(structured_output=False)
    async def click(instance_id: str, frame_id: str, x: int, y: int, count: int = 1):
        """在对应截图坐标单击或双击(count=2)，返回新截图；不抢占真实鼠标。"""
        return await invoke(controller.action, instance_id, "click", frame_id=frame_id, points=[[x,y]], count=count)

    @server.tool(structured_output=False)
    async def long_press(instance_id: str, frame_id: str, x: int, y: int, duration: float = 1):
        """长按 0.01 到 10 秒，并在结束或取消时释放。"""
        return await invoke(controller.action, instance_id, "long_press", frame_id=frame_id, points=[[x,y]], duration=duration)

    @server.tool(structured_output=False)
    async def swipe(instance_id: str, frame_id: str, x1: int, y1: int, x2: int, y2: int, duration: float = .4):
        """按住并连续移动到终点后释放，用于滑动或拖动；返回操作后截图。"""
        return await invoke(controller.action, instance_id, "swipe", frame_id=frame_id,
                            points=[[x1,y1],[x2,y2]], duration=duration)

    @server.tool(structured_output=False)
    async def press_key(instance_id: str, key: str):
        """雷电仅 home/recent；MuMu 支持 back/home/recent/enter/volume_up/volume_down。"""
        return await invoke(controller.action, instance_id, "press_key", key=key)

    @server.tool(structured_output=False)
    async def input_text(instance_id: str, text: str):
        """使用 MuMu 原生接口输入 UTF-8 文本。雷电尚未验证此能力，会明确拒绝。"""
        return await invoke(controller.action, instance_id, "input_text", text=text)

    @server.tool(structured_output=False)
    async def cancel(instance_id: str):
        """取消指定实例持续输入与排队操作；不关闭模拟器。"""
        return await invoke(controller.cancel, instance_id)

    return server
