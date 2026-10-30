"""Experimental Codex WebRTC session lifecycle for the opt-in XiaoZhi route.

This component owns the subprocess it starts. It does not implement XiaoZhi
audio, authentication for devices, task dispatch, or a production server.
"""

import asyncio
import json
import os
import signal
from pathlib import Path


class CodexSessionError(RuntimeError):
    pass


class CodexRealtimeSession:
    def __init__(self, workspace: Path, model: str = "gpt-6.1-terra", developer_instructions: str = "", *, codex_home=None,
                 conversation=None, tool_handler=None, dynamic_tools=None, worker=False, device_tool_server=None):
        self.workspace = workspace.resolve()
        self.codex_home = Path(codex_home).resolve() if codex_home is not None else None
        self.model = model
        self.developer_instructions = developer_instructions
        self.conversation = conversation
        self.tool_handler = tool_handler
        self.dynamic_tools = dynamic_tools
        self.device_tool_server = device_tool_server
        self.confirmed_model = None
        self.device_inventory = None
        self.worker = worker
        self.server_requests = set()
        self.realtime_started = False
        self.control_turns = set()
        self.tool_failures = set()
        self.process = None
        self.reader = None
        self.pending = {}
        self.serial = 0
        self.thread_id = None
        self.answer = None
        self.events = asyncio.Queue(maxsize=128)
        self.event_overflow = False

    async def _send(self, message):
        if not self.process or not self.process.stdin or self.process.returncode is not None:
            raise CodexSessionError("Codex process is not running")
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def _request(self, method, params, timeout=30):
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise CodexSessionError(f"Codex request failed: {method}")
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = self.pending.get(request_id)
                    if future and not future.done():
                        future.set_result(message)
                elif request_id is not None and "method" in message:
                    if self.tool_handler is not None and message["method"] == "item/tool/call" and len(self.server_requests)<16:
                        self.control_turns.add(message.get('params',{}).get('turnId'))
                        task=asyncio.create_task(self._handle_tool(message))
                        self.server_requests.add(task)
                        task.add_done_callback(self.server_requests.discard)
                    else:
                        await self._send({"id": request_id, "error": {"code": -32601, "message": "Unsupported server request"}})
                else:
                    method = message.get("method", "")
                    params = message.get("params", {})
                    if method == "thread/realtime/sdp" and self.answer and not self.answer.done():
                        self.answer.set_result(params["sdp"])
                    elif method.startswith("thread/realtime/") or method in ("account/login/completed", "account/updated", "turn/started", "turn/completed", "item/completed"):
                        if method in ("thread/realtime/error", "thread/realtime/closed") and self.answer and not self.answer.done():
                            self.answer.set_exception(CodexSessionError("realtime startup was rejected or closed"))
                        try:
                            self.events.put_nowait(message)
                        except asyncio.QueueFull:
                            self.event_overflow = True
                            if self.process.returncode is None:
                                self.process.terminate()
                            break
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexSessionError("Codex process closed"))
            if self.answer and not self.answer.done():
                self.answer.set_exception(CodexSessionError("Codex process closed"))

    async def _handle_tool(self,message):
        try:
            params=message.get('params',{})
            if params.get('threadId')!=self.thread_id:raise ValueError('foreign thread')
            result=await self.tool_handler(params)
            if isinstance(result,dict) and result.get('accepted') is False:self.control_turns.discard(params.get('turnId'))
            await self._send({'id':message['id'],'result':{'contentItems':[{'type':'inputText','text':json.dumps(result,ensure_ascii=False)}],'success':True}})
        except asyncio.CancelledError:raise
        except Exception:
            self.tool_failures.add(message.get('params',{}).get('turnId'))
            try:await self._send({'id':message['id'],'result':{'contentItems':[{'type':'inputText','text':'任务操作失败，未确认执行成功。'}],'success':False}})
            except CodexSessionError:pass

    async def open(self):
        if self.process:
            raise CodexSessionError("session already started")
        if not self.workspace.is_dir():
            raise CodexSessionError("workspace is required")
        env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY") and k not in ("CODEX_ACCESS_TOKEN", "OPENAI_AUTH_TOKEN")}
        if self.worker:
            allowed={'PATH','HOME','USER','LOGNAME','SHELL','TMPDIR','LANG','LC_ALL','LC_CTYPE','TZ',
                'SSL_CERT_FILE','SSL_CERT_DIR','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','NO_PROXY',
                'https_proxy','http_proxy','all_proxy','no_proxy'}
            env={key:value for key,value in env.items() if key in allowed}
        command = ["codex", "app-server", "--listen", "stdio://"]
        if self.codex_home is not None:
            if not self.codex_home.is_dir():
                raise CodexSessionError("isolated profile is required")
            env["CODEX_HOME"] = str(self.codex_home)
            command.extend(["-c", 'cli_auth_credentials_store="file"'])
        for feature in ("apps", "plugins", "hooks", "memories", "chronicle", "browser_use", "computer_use", "multi_agent"):
            command.extend(["-c", f"features.{feature}=false"])
        if self.tool_handler is not None:command.extend(['-c','features.shell_tool=false'])
        if self.device_tool_server:
            command.extend(['-c','web_search="live"'])
            mcp=self.device_tool_server
            for key in ('command','args'):
                command.extend(['-c','mcp_servers.xiaozhi_device.'+key+'='+json.dumps(mcp[key])])
            from .device_mcp import ALLOWED
            for name in [item.removeprefix('self.').replace('.','_') for item in ALLOWED]+['model_info']:
                command.extend(['-c','mcp_servers.xiaozhi_device.tools.'+name+'.approval_mode="approve"'])
            values=','.join(key+'='+json.dumps(value) for key,value in mcp['env'].items())
            command.extend(['-c','mcp_servers.xiaozhi_device.env={'+values+'}'])
        if self.worker:
            command.extend(['-c','sandbox_workspace_write.network_access=false','-c','web_search="disabled"'])
        self.process = await asyncio.create_subprocess_exec(
            *command, cwd=str(self.workspace),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=self.worker,
        )
        self.reader = asyncio.create_task(self._read())
        try:
            await self._request("initialize", {"clientInfo": {"name": "xiaozhi_codex_realtime", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
            await self._send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def start(self, offer_sdp: str) -> str:
        if not offer_sdp:
            raise CodexSessionError("WebRTC offer is required")
        await self.open()
        try:
            if self.device_tool_server:
                status=await self._request('mcpServerStatus/list',{},timeout=15)
                device=next((item for item in status.get('data',[]) if item.get('name')=='xiaozhi_device'),None)
                if not device or device.get('toolsError') or 'model_info' not in device.get('tools',{}):
                    raise CodexSessionError('device MCP tools failed to initialize')
            account = (await self._request("account/read", {"refreshToken": False})).get("account") or {}
            if account.get("type") != "chatgpt":
                raise CodexSessionError("ChatGPT login is required")
            saved = self.conversation.thread() if self.conversation else None
            params = {
                "cwd": str(self.workspace), "model": self.model,
                "sandbox": "read-only", "approvalPolicy": "never",
                "runtimeWorkspaceRoots": [str(self.workspace)],
                "developerInstructions": self.developer_instructions,
            }
            if self.device_tool_server:params['config']={'web_search':'live'}
            if saved:params.update(threadId=saved,excludeTurns=True)
            else:params.update(ephemeral=self.conversation is None,allowProviderModelFallback=False)
            if self.dynamic_tools and not saved:params['dynamicTools']=self.dynamic_tools
            thread = await self._request('thread/resume' if saved else 'thread/start',params)
            if thread.get("model") != self.model:
                raise CodexSessionError("requested model was not selected")
            self.confirmed_model = thread["model"]
            self.thread_id = thread["thread"]["id"]
            if saved and saved!=self.thread_id:raise CodexSessionError('resume returned a different thread')
            if self.conversation:self.conversation.save_thread(self.thread_id)
            device_inventory=None
            if self.device_tool_server:
                inventory=await self._request('mcpServerStatus/list',{'threadId':self.thread_id,'serverName':'xiaozhi_device'},timeout=15)
                device_inventory=next((item for item in inventory.get('data',[]) if item.get('name')=='xiaozhi_device'),None)
                if not device_inventory or device_inventory.get('toolsError') or 'model_info' not in device_inventory.get('tools',{}):
                    raise CodexSessionError('resumed thread device MCP tools unavailable')
            self.device_inventory = device_inventory
            self.answer = asyncio.get_running_loop().create_future()
            initial=self.conversation.initial_items() if self.conversation else []
            if device_inventory:
                initial.append({'role':'developer','text':'Connection identity verified by thread/start or thread/resume: the backing reasoning model is '+self.confirmed_model+'. The native realtime acoustic model identifier is not disclosed. You may answer a current backing-model identity question directly using this verified identity, without waiting for delegation. For background task execution models, query model_info. Current verified device MCP tools on this same conversation: '+', '.join(device_inventory['tools'])+'. For device control/status or model queries, delegate to backing Codex to call these tools. Previous statements that device control/model lookup is unavailable are obsolete.'})
            await self._request("thread/realtime/start", {
                "threadId": self.thread_id, "version": "v3", "outputModality": "audio",
                "transport": {"type": "webrtc", "sdp": offer_sdp},
                "includeStartupContext": not bool(initial), "clientManagedHandoffs": True,
                "initialItems": initial,"realtimeStartInstructions":self.developer_instructions,
            })
            self.realtime_started = True
            return await asyncio.wait_for(self.answer, 40)
        except BaseException:
            if self.answer is not None:
                if not self.answer.done():
                    self.answer.cancel()
                elif not self.answer.cancelled():
                    self.answer.exception()
            await self.close()
            raise

    async def close(self):
        for task in self.server_requests:task.cancel()
        await asyncio.gather(*self.server_requests,return_exceptions=True)
        if self.process and self.process.returncode is None:
            if self.thread_id and self.realtime_started:
                try:
                    await self._request("thread/realtime/stop", {"threadId": self.thread_id}, 5)
                except (CodexSessionError, asyncio.TimeoutError):
                    pass
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
            if self.worker and getattr(self.process,'pid',None):
                # Only the process group created by this worker; never global PIDs.
                try:os.killpg(self.process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
