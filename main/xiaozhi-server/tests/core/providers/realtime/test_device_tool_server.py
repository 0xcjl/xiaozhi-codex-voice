import asyncio
import json
import unittest
from unittest.mock import AsyncMock
from core.providers.realtime.device_tool_server import DeviceToolServer

class ToolServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.handler=AsyncMock(return_value={'tools':[]})
        self.server=DeviceToolServer(self.handler);self.config=await self.server.start()
    async def asyncTearDown(self):await self.server.close()
    async def call(self,secret):
        reader,writer=await asyncio.open_connection('127.0.0.1',int(self.config['env']['XIAOZHI_TOOL_PORT']))
        writer.write((json.dumps({'secret':secret,'method':'tools/list','params':{}})+'\n').encode());await writer.drain()
        reply=await reader.readline();writer.close();await writer.wait_closed();return reply
    async def test_nonce_is_required_and_bound_to_connection(self):
        self.assertEqual(await self.call('wrong'),b'');self.handler.assert_not_awaited()
        reply=json.loads(await self.call(self.config['env']['XIAOZHI_TOOL_SECRET']))
        self.assertEqual(reply,{'result':{'tools':[]}});self.handler.assert_awaited_once()
    async def test_real_stdio_process_forwards_tools_and_closes(self):
        process=await asyncio.create_subprocess_exec(self.config['command'],*self.config['args'],env=self.config['env'],stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE)
        output,_=await process.communicate(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        self.assertEqual(process.returncode,0);self.assertEqual(json.loads(output)['result'],{'tools':[]})

    async def test_private_control_manifest_is_removed_when_owned_connection_closes(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'device.json'
            server=DeviceToolServer(self.handler);await server.start(path)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            await server.close();self.assertFalse(path.exists())
