import asyncio
import unittest
from unittest.mock import AsyncMock
from core.providers.realtime.device_mcp import DeviceMcp

class DeviceMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent=[];self.verify=AsyncMock()
        async def send(message):self.sent.append(message)
        self.mcp=DeviceMcp(send,self.verify,timeout=.05)
    async def request(self,name,args=None):
        task=asyncio.create_task(self.mcp.call(name,args or {}));await asyncio.sleep(0)
        return task,self.sent[-1]['payload']['id']
    def respond(self,ident,result):self.mcp.receive({'type':'mcp','payload':{'jsonrpc':'2.0','id':ident,'result':result}})
    async def test_volume_requires_advertised_tool_and_valid_integer(self):
        self.mcp.tools={'self.audio_speaker.set_volume':{'inputSchema':{'type':'object','properties':{'volume':{'type':'integer','minimum':0,'maximum':100}},'required':['volume']}}}
        for value in (-1,101,True,3.5):
            with self.assertRaises(ValueError):await self.mcp.call('self.audio_speaker.set_volume',{'volume':value})
        task,ident=await self.request('self.audio_speaker.set_volume',{'volume':46})
        self.respond(ident,{'content':[{'type':'text','text':'true'}]});await task
        self.verify.assert_awaited()
    async def test_firmware_and_unadvertised_tools_never_sent(self):
        for name in ('self.upgrade_firmware','self.reboot','self.assets.set_download_url','unknown'):
            with self.assertRaises(ValueError):await self.mcp.call(name,{})
        self.assertEqual(self.sent,[])
    async def test_correlated_replies_and_late_unknown_reply(self):
        self.mcp.tools={'self.get_device_status':{'inputSchema':{'properties':{}}}}
        a,first=await self.request('self.get_device_status');b,second=await self.request('self.get_device_status')
        self.respond(second,{'value':2});self.respond(first,{'value':1})
        self.assertEqual(await a,{'value':1});self.assertEqual(await b,{'value':2})
        self.respond(first,{'value':'late'});self.assertFalse(self.mcp.pending)
    async def test_timeout_does_not_retry_mutation(self):
        self.mcp.tools={'self.start_wifi_config':{'inputSchema':{'properties':{}}}}
        with self.assertRaises(TimeoutError):await self.mcp.call('self.start_wifi_config',{})
        self.assertEqual(len(self.sent),1);self.assertFalse(self.mcp.pending)
    async def test_revocation_blocks_call(self):
        self.mcp.tools={'self.get_device_status':{'inputSchema':{'properties':{}}}}
        self.verify.side_effect=PermissionError('revoked')
        with self.assertRaises(PermissionError):await self.mcp.call('self.get_device_status',{})
        self.assertEqual(self.sent,[])
    async def test_disconnect_releases_waiters(self):
        self.mcp.tools={'self.get_device_status':{'inputSchema':{'properties':{}}}}
        task,_=await self.request('self.get_device_status');self.mcp.close()
        with self.assertRaises(ConnectionError):await task
