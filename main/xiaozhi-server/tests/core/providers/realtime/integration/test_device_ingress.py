import unittest,asyncio
from aiohttp import web,WSMsgType,WSServerHandshakeError
from aiohttp.test_utils import TestServer,TestClient
from yarl import URL
from core.providers.realtime.device_ingress import DeviceIngress,public_origin

TOKEN='native2.'+'a'*43
HEADERS={'Device-Id':'02:00:00:00:00:01','Client-Id':'synthetic-client','Protocol-Version':'2','Authorization':'Bearer '+TOKEN,'Cookie':'admin=must-not-forward','X-Forwarded-For':'untrusted'}

class DeviceIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[];self.ota_mode='valid';self.ws_mode='echo'
        app=web.Application()
        async def ota(request):
            self.requests.append(('ota',dict(request.headers)))
            if self.ota_mode=='deny':return web.json_response({'token':'must-not-reflect'},status=403)
            if self.ota_mode=='redirect':raise web.HTTPFound(self.base+'/forbidden')
            data={'websocket':{'url':self.base.replace('http:','ws:')+'/xiaozhi/v1/','token':TOKEN,'version':2}}
            if self.ota_mode=='firmware':data['firmware']={'url':'must-not-forward'}
            if self.ota_mode=='wrong_token':data['websocket']['token']='other-credential'
            return web.json_response(data)
        async def voice(request):
            self.requests.append(('voice',dict(request.headers)))
            if self.ws_mode=='redirect':raise web.HTTPFound(self.base+'/forbidden')
            socket=web.WebSocketResponse(compress=False);await socket.prepare(request)
            if self.ws_mode=='deny':await socket.close(code=1008);return socket
            async for message in socket:
                if message.type==WSMsgType.TEXT:await socket.send_str(message.data)
                elif message.type==WSMsgType.BINARY:await socket.send_bytes(message.data)
            return socket
        async def forbidden(_):self.fail('redirect target was requested')
        app.router.add_post('/xiaozhi/ota/native/{token}',ota)
        app.router.add_get('/xiaozhi/v1/',voice)
        app.router.add_route('*','/forbidden',forbidden)
        self.upstream=TestServer(app);await self.upstream.start_server();self.base=str(self.upstream.make_url('')).rstrip('/')
        self.ingress=DeviceIngress('https://device.example.invalid',self.base,self.base.replace('http:','ws:')+'/xiaozhi/v1/')
        self.client=TestClient(TestServer(self.ingress.app()));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();await self.upstream.close()
    async def test_ota_is_sanitized_and_advertises_only_public_wss(self):
        r=await self.client.post('/xiaozhi/ota/native/'+TOKEN,headers=HEADERS,json={'application':{'version':'2.0.5'}})
        self.assertEqual(r.status,200);data=await r.json()
        self.assertEqual(data,{'websocket':{'url':'wss://device.example.invalid/xiaozhi/v1/','token':TOKEN,'version':2}})
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.assertNotIn('Cookie',self.requests[0][1]);self.assertNotIn('Authorization',self.requests[0][1])
    async def test_console_account_and_anonymous_ota_are_not_routes(self):
        for path in ['/','/xiaozhi/service-connections','/account','/config','/xiaozhi/ota/','/xiaozhi/login']:
            r=await self.client.get(path,headers=HEADERS);self.assertEqual(r.status,404)
        self.assertFalse(self.requests)
    async def test_missing_or_malformed_device_identity_never_reaches_upstream(self):
        for headers in [{},{**HEADERS,'Device-Id':'other'},{**HEADERS,'Client-Id':''}]:
            r=await self.client.post('/xiaozhi/ota/native/'+TOKEN,headers=headers);self.assertEqual(r.status,403)
        r=await self.client.post('/xiaozhi/ota/native/not-a-grant',headers=HEADERS);self.assertEqual(r.status,403)
        self.assertFalse(self.requests)
    async def test_query_credentials_and_encoded_paths_are_rejected(self):
        for path in ['/xiaozhi/ota/native/'+TOKEN+'?debug=1','/xiaozhi/%6fta/native/'+TOKEN]:
            r=await self.client.post(URL(path,encoded=True),headers=HEADERS);self.assertEqual(r.status,404)
    async def test_firmware_or_wrong_credentials_are_not_forwarded(self):
        for mode in ['firmware','wrong_token']:
            self.ota_mode=mode;r=await self.client.post('/xiaozhi/ota/native/'+TOKEN,headers=HEADERS)
            self.assertEqual(r.status,502);self.assertNotIn(TOKEN,await r.text())
    async def test_ota_denials_and_redirects_do_not_reflect_upstream_data(self):
        for mode,status in [('deny',403),('redirect',502)]:
            self.ota_mode=mode;r=await self.client.post('/xiaozhi/ota/native/'+TOKEN,headers=HEADERS)
            self.assertEqual(r.status,status);self.assertNotIn('must-not-reflect',await r.text())
    async def test_ota_body_is_bounded(self):
        r=await self.client.post('/xiaozhi/ota/native/'+TOKEN,headers=HEADERS,data=b'x'*16385)
        self.assertEqual(r.status,413);self.assertFalse(self.requests)
    async def test_websocket_relays_text_binary_and_only_identity_headers(self):
        async with self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS) as ws:
            await ws.send_str('{"type":"hello"}');self.assertEqual((await ws.receive()).data,'{"type":"hello"}')
            await ws.send_bytes(b'bounded-opus-fixture');self.assertEqual((await ws.receive()).data,b'bounded-opus-fixture')
        h=self.requests[-1][1];self.assertEqual(h['Authorization'],'Bearer '+TOKEN)
        self.assertNotIn('Cookie',h);self.assertNotIn('X-Forwarded-For',h)
    async def test_missing_websocket_credential_rejected_before_upstream(self):
        with self.assertRaises(WSServerHandshakeError) as error:await self.client.ws_connect('/xiaozhi/v1/',headers={})
        self.assertEqual(error.exception.status,403);self.assertFalse(self.requests)
    async def test_upstream_revocation_close_code_is_preserved(self):
        self.ws_mode='deny'
        async with self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS) as ws:
            await ws.receive();self.assertEqual(ws.close_code,1008)
    async def test_capacity_denies_ninth_socket_and_releases_after_close(self):
        sockets=[]
        try:
            for _ in range(8):sockets.append(await self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS))
            with self.assertRaises(WSServerHandshakeError) as error:await self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS)
            self.assertEqual(error.exception.status,503)
            await sockets.pop().close()
            async with asyncio.timeout(1):
                while self.ingress.slots.locked():await asyncio.sleep(.001)
            sockets.append(await self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS))
        finally:
            for socket in sockets:await socket.close()
    async def test_oversized_websocket_frame_is_closed(self):
        async with self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS) as ws:
            await ws.send_bytes(b'x'*16385);await ws.receive()
            self.assertEqual(ws.close_code,1009)
    async def test_websocket_redirect_never_follows_to_another_endpoint(self):
        self.ws_mode='redirect'
        with self.assertRaises(WSServerHandshakeError) as error:await self.client.ws_connect('/xiaozhi/v1/',headers=HEADERS)
        self.assertEqual(error.exception.status,502)
    async def test_insecure_or_credential_bearing_public_origin_is_rejected(self):
        for value in ['http://device.example','https://user:pass@device.example','https://device.example/path','https://device.example/?token=secret','https://device.example\n']:
            with self.assertRaises(ValueError):public_origin(value)
