"""Device-only loopback origin for a separately authorized HTTPS/WSS edge.

Never exposes the console, account control or management API. This module does
not create a tunnel, issue device credentials, or change device configuration.
"""
import argparse
import asyncio
import json
import logging
import re
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

TOKEN=re.compile(r'native2\.[A-Za-z0-9_-]{43}\Z')
MAC=re.compile(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\Z')
CLIENT=re.compile(r'[A-Za-z0-9:_-]{1,128}\Z')
FRAME_LIMIT=16384


def public_origin(value):
    u=urlsplit(value)
    if (u.scheme!='https' or not u.hostname or u.username or u.password
            or u.path not in ('','/') or u.query or u.fragment or '%' in value
            or re.search(r'[\x00-\x20\x7f]',value) or u.port==0):
        raise ValueError('an HTTPS origin without credentials or a path is required')
    if len(value.rstrip('/')+'/xiaozhi/ota/native/'+'native2.'+'x'*43)>255:
        raise ValueError('device OTA address exceeds the firmware limit')
    return value.rstrip('/')


def loopback(value,scheme,path):
    u=urlsplit(value)
    if (u.scheme!=scheme or u.hostname!='127.0.0.1' or u.username or u.password
            or u.path!=path or u.query or u.fragment):
        raise ValueError('upstream must be an exact loopback endpoint')
    return value


class DeviceIngress:
    def __init__(self,origin,management='http://127.0.0.1:18821',voice='ws://127.0.0.1:18824/xiaozhi/v1/'):
        self.origin=public_origin(origin)
        self.management=loopback(management,'http','')
        self.voice=loopback(voice,'ws','/xiaozhi/v1/')
        self.external_voice='wss://'+urlsplit(self.origin).netloc+'/xiaozhi/v1/'
        self.slots=asyncio.Semaphore(8)
        self.client=None

    @staticmethod
    def deny(status):
        return web.json_response({'error':'device_request_denied'},status=status,
            headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @staticmethod
    def identity(request):
        mac=request.headers.get('Device-Id','');client=request.headers.get('Client-Id','')
        if not MAC.fullmatch(mac) or not CLIENT.fullmatch(client):return None
        return {'Device-Id':mac,'Client-Id':client}

    async def ota(self,request):
        token=request.match_info['token'];headers=self.identity(request)
        if not TOKEN.fullmatch(token) or headers is None:return self.deny(403)
        if request.content_length is not None and request.content_length>FRAME_LIMIT:return self.deny(413)
        if self.slots.locked():return self.deny(503)
        async with self.slots:
            # Authenticate first; unauthenticated slow bodies cannot hold a slot.
            # The native OTA handler needs only identity headers, not device reports.
            async with self.client.post(self.management+'/xiaozhi/ota/native/'+token,
                    headers=headers,allow_redirects=False,timeout=aiohttp.ClientTimeout(total=8)) as upstream:
                if upstream.status!=200:return self.deny(403 if upstream.status in (401,403) else 502)
                raw=await upstream.content.read(8193)
                if len(raw)>8192:return self.deny(502)
                data=json.loads(raw)
                ws=data.get('websocket',{})
                if (set(data)!={'websocket'} or not isinstance(ws,dict)
                        or ws.get('url') not in (self.voice,self.external_voice)
                        or ws.get('token')!=token or ws.get('version')!=2):return self.deny(502)
                async with asyncio.timeout(8):await request.read()
                return web.json_response({'websocket':{'url':self.external_voice,'token':token,'version':2}},
                    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    async def voice_socket(self,request):
        headers=self.identity(request);auth=request.headers.get('Authorization','')
        version=request.headers.get('Protocol-Version','')
        if (headers is None or not auth.startswith('Bearer ') or not TOKEN.fullmatch(auth[7:])
                or version not in ('1','2','3')):return self.deny(403)
        if self.slots.locked():return self.deny(503)
        headers.update(Authorization=auth,**{'Protocol-Version':version})
        async with self.slots:
            async with self.client.ws_connect(self.voice,headers=headers,compress=0,
                    max_msg_size=FRAME_LIMIT,autoping=True) as upstream:
                device=web.WebSocketResponse(compress=False,max_msg_size=FRAME_LIMIT)
                await device.prepare(request)
                async def relay(source,target):
                    async for message in source:
                        if message.type==aiohttp.WSMsgType.TEXT:await target.send_str(message.data)
                        elif message.type==aiohttp.WSMsgType.BINARY:await target.send_bytes(message.data)
                        else:break
                tasks=[asyncio.create_task(relay(device,upstream)),asyncio.create_task(relay(upstream,device))]
                try:
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    failed=any(not t.cancelled() and t.exception() is not None for t in done)
                    code=upstream.close_code or (1011 if failed else 1000)
                    if code not in (1000,1001,1008,1009,1011,1013):code=1011
                    await device.close(code=code)
                finally:
                    for task in tasks:task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    await upstream.close()
                return device

    def app(self):
        @web.middleware
        async def guard(request,handler):
            if '?' in request.raw_path or '%' in request.raw_path:return self.deny(404)
            try:return await handler(request)
            except web.HTTPException as error:return self.deny(error.status)
            except (aiohttp.ClientError,TimeoutError,OSError,ValueError,TypeError,AttributeError):
                # Upstream errors can contain credential-bearing URLs.
                return self.deny(502)
        app=web.Application(client_max_size=FRAME_LIMIT,middlewares=[guard])
        app.router.add_post('/xiaozhi/ota/native/{token}',self.ota)
        app.router.add_get('/xiaozhi/v1/',self.voice_socket)
        async def lifecycle(_):
            trace=aiohttp.TraceConfig()
            async def reject_redirect(*_):raise aiohttp.ClientError('upstream redirect denied')
            trace.on_request_redirect.append(reject_redirect)
            async with aiohttp.ClientSession(trust_env=False,connector=aiohttp.TCPConnector(limit=16),
                    cookie_jar=aiohttp.DummyCookieJar(),trace_configs=[trace],
                    timeout=aiohttp.ClientTimeout(total=None,sock_connect=5,sock_read=8)) as client:
                self.client=client
                yield
        app.cleanup_ctx.append(lifecycle)
        return app


async def serve(origin,port):
    # No request URLs or credential-bearing error exceptions are logged.
    logger=logging.Logger('xiaozhi.device_ingress');logger.disabled=True
    runner=web.AppRunner(DeviceIngress(origin).app(),access_log=None,logger=logger)
    await runner.setup()
    try:
        await web.TCPSite(runner,'127.0.0.1',port).start()
        print('Device-only ingress listening on loopback; no external tunnel started',flush=True)
        await asyncio.Future()
    finally:await runner.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--public-origin',required=True)
    parser.add_argument('--port',type=int,default=18825)
    args=parser.parse_args()
    try:asyncio.run(serve(args.public_origin,args.port))
    except KeyboardInterrupt:pass
