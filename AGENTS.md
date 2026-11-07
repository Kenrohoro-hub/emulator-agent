# 项目规则

- 本目录为 `<PROJECT_ROOT>`。这是 Windows 本地 MCP 控制工具，只控制雷电和 MuMu。
- 不调用 ADB、ADB 服务、scrcpy 或经 ADB 部署的触控组件；不能静默使用真实鼠标、剪贴板或抢占前台焦点。
- 截图和输入不得把“调用返回成功”表述为业务完成。超时后先观察，不自动重复有副作用的操作。
- 每个原生后端放入本工具创建的独立工作进程。只回收自有子进程；模拟器启停走其管理程序，禁止按标题或进程名盲杀。
- 坐标依赖返回截图的编号，裁剪、缩放、实例代次变化由控制层处理。厂商 DLL 输出不得进入 MCP stdout。
- `STATE.md` 是唯一能力和验证状态入口。README 只说明用法与稳定边界。
- `runtime/`、`.venv/`、`.dev/`、`config.local.json` 不入 Git；不将截图、设备标识、输入文本或凭据写进规则和提交。
- 测试以当前核心流程为限。`tests/mcp_smoke.py` 只读；`tests/live_validation.py` 会操作真实实例，显式运行，不加入普通单元测试自动发现。
- Windows 运行入口为 ASCII 的 `run-app.cmd`。中文使用说明在 README。
