"""Read-only live MCP check. Run explicitly on a Windows emulator host."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    root=Path(__file__).resolve().parent.parent
    params=StdioServerParameters(command=sys.executable,args=['-X','utf8',str(root/'main.py'),'serve'])
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
                if not item['running']:continue
                frame=await session.call_tool('screenshot',{'instance_id':item['instance_id'],'max_size':800})
                assert not frame.isError,frame
                assert len(frame.content)==2 and frame.content[1].type=='image'
                assert json.loads(frame.content[0].text)['coordinate_space']=='returned_image'
                invalid=await session.call_tool('click',{'instance_id':item['instance_id'],'frame_id':'invalid','x':1,'y':1})
                assert invalid.isError
                count+=1
            assert count>0,'需要至少一个已运行实例'
            print(f'MCP handshake, 10 tools, {count} images and invalid-frame rejection passed.')


if __name__=='__main__':asyncio.run(main())
