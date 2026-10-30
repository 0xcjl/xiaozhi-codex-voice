"""Private per-connection IPC and stdio MCP adapter for resumed Codex threads.

No files, credentials or persistent Codex configuration are modified.
"""
import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import sys


class DeviceToolServer:
    def __init__(self,handler):
        self.handler=handler;self.secret=secrets.token_urlsafe(32)
        self.server=None;self.clients=set();self.manifest=None
    async def start(self,manifest=None):
        self.server=await asyncio.start_server(self._client,'127.0.0.1',0,limit=32768)
        port=self.server.sockets[0].getsockname()[1]
        if manifest is not None:
            self.manifest=Path(manifest)
            fd=os.open(self.manifest,os.O_CREAT|os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:json.dump({'port':port,'secret':self.secret},stream)
        return {'command':sys.executable,'args':[str(Path(__file__).resolve())],
                'env':{'XIAOZHI_TOOL_PORT':str(port),'XIAOZHI_TOOL_SECRET':self.secret}}
    async def _client(self,reader,writer):
        task=asyncio.current_task();self.clients.add(task)
        try:
            if len(self.clients)>8:return
            request=json.loads(await asyncio.wait_for(reader.readline(),3))
            if not isinstance(request,dict) or not isinstance(request.get('secret'),str) or not hmac.compare_digest(request['secret'],self.secret):return
            async with asyncio.timeout(15):result=await self.handler(request.get('method'),request.get('params',{}))
            writer.write((json.dumps({'result':result})+'\n').encode());await writer.drain()
        except asyncio.CancelledError:raise
        except Exception as error:
            category=type(error).__name__
            writer.write((json.dumps({'error':'Device operation unconfirmed; do not claim success.','errorType':category})+'\n').encode())
            try:await writer.drain()
            except (ConnectionError,OSError):pass
        finally:
            writer.close();await writer.wait_closed();self.clients.discard(task)
    async def close(self):
        if self.server:self.server.close();await self.server.wait_closed()
        for task in list(self.clients):task.cancel()
        await asyncio.gather(*list(self.clients),return_exceptions=True)
        if self.manifest and self.manifest.is_file() and not self.manifest.is_symlink():
            value=json.loads(self.manifest.read_text())
            if value.get('secret')==self.secret:self.manifest.unlink()


def forward(method,params):
    import socket
    request={'secret':os.environ['XIAOZHI_TOOL_SECRET'],'method':method,'params':params}
    with socket.create_connection(('127.0.0.1',int(os.environ['XIAOZHI_TOOL_PORT'])),timeout=18) as sock:
        sock.sendall((json.dumps(request)+'\n').encode())
        with sock.makefile('rb') as stream:reply=json.loads(stream.readline(32768))
    if 'error' in reply:raise ValueError('device operation unconfirmed')
    return reply['result']


def stdio():
    for line in sys.stdin:
        ident=None
        try:
            if len(line)>32768:raise ValueError('oversized MCP message')
            request=json.loads(line);ident=request.get('id');method=request.get('method')
            if ident is None:continue
            if method=='initialize':result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'xiaozhi-device','version':'1'}}
            elif method=='ping':result={}
            elif method in ('tools/list','tools/call'):result=forward(method,request.get('params',{}))
            else:raise ValueError('unsupported MCP request')
            response={'jsonrpc':'2.0','id':ident,'result':result}
        except Exception:
            response={'jsonrpc':'2.0','id':ident,'error':{'code':-32603,'message':'Device operation unconfirmed; no success asserted.'}}
        if ident is not None:print(json.dumps(response),flush=True)

if __name__=='__main__':stdio()
