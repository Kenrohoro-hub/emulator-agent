"""Read-only live MCP check. Run explicitly on a Windows emulator host."""
import asyncio
import base64
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from emulator_agent import __version__


async def main():
    root=Path(__file__).resolve().parent.parent
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(root/'main.py'),'serve'])
    report={'tool_version':__version__,'checked_at':datetime.now(timezone.utc).isoformat(),'checks':[]}
    async with stdio_client(params) as (read,write):
        async with ClientSession(read,write) as session:
            await session.initialize()
            tools=await session.list_tools()
            assert len(tools.tools)==10
            result=await session.call_tool('list_instances',{})
            assert not result.isError,result
            items=json.loads(result.content[0].text)
            count=0
            for item in items:
                if item.get('state')!='running':continue
                info=await session.call_tool('get_instance',{'instance_id':item['instance_id']})
                assert not info.isError,info
                assert json.loads(info.content[0].text)['tool_version']==__version__
                frame=await session.call_tool('screenshot',{'instance_id':item['instance_id'],'max_size':800})
                assert not frame.isError,frame
                assert len(frame.content)==2 and frame.content[1].type=='image'
                assert json.loads(frame.content[0].text)['coordinate_space']=='returned_image'
                for options in ({'max_size':4096},{'crop':[0,0,128,128],'max_size':128}):
                    capture=await session.call_tool('screenshot',{'instance_id':item['instance_id'],**options})
                    assert not capture.isError,capture
                    meta=json.loads(capture.content[0].text)
                    with Image.open(BytesIO(base64.b64decode(capture.content[1].data))) as image:
                        assert image.size==(meta['width'],meta['height'])
                    if 'crop' in options:assert (meta['width'],meta['height'])==(128,128)
                invalid=await session.call_tool('click',{'instance_id':item['instance_id'],'frame_id':'invalid','x':1,'y':1})
                assert invalid.isError
                count+=1
                report['checks'].append({'instance_id':item['instance_id'],'state':item['state'],
                                         'full_resize_crop_and_invalid_frame':'passed'})
            assert count>0,'需要至少一个可确认运行的实例；受限查询应返回 unknown，不能据此启动实例'
            report['status']='passed'
            output=root/'runtime/validation/mcp-smoke.json'
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'MCP {__version__}: handshake, 10 tools, {count} running instances, full/resize/crop images and invalid-frame rejection passed.')


if __name__=='__main__':asyncio.run(main())
