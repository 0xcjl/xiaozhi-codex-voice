"""Opt-in Codex realtime route for the existing XiaoZhi WebSocket service.

No credentials or transcripts are logged. Device selection is server-owned.
Task dispatch and conversation persistence are explicitly opt-in per runtime.
"""

import asyncio
import json
import logging
import time
import re
from pathlib import Path
from uuid import uuid4

from .device_session import DeviceSession
from .playback import DevicePlayback
from .xiaozhi_protocol import MAX_AUDIO_PACKET_BYTES, parse_hello


async def lookup_console_connection(config, device_id):
    # One bounded read, no automatic retry or provider-secret transfer.
    from config.manage_api_client import ManageApiClient
    instance = ManageApiClient._get_instance()
    if instance is None:
        raise ConnectionError("console client is not initialized")
    async with instance._build_client() as client:
        return await asyncio.wait_for(instance._async_request(
            client, "POST", "/config/service-connection", json={"macAddress": device_id}
        ), 4)


async def verify_console_device(token, device_id, client_id):
    from config.manage_api_client import ManageApiClient
    instance = ManageApiClient._get_instance()
    if instance is None:
        raise ConnectionError("console client is not initialized")
    async with instance._build_client() as client:
        return await asyncio.wait_for(instance._async_request(
            client, "POST", "/config/native-device-access",
            json={"token": token, "macAddress": device_id, "clientId": client_id}
        ), 4)


async def run_with_access_guard(connection, verify_access, interval=10):
    """Recheck revocation while connected; fail closed on management outage."""
    async def watch():
        while True:
            await asyncio.sleep(interval)
            await verify_access()
    running = asyncio.create_task(connection.run())
    watching = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait({running, watching}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        running.cancel()
        watching.cancel()
        await asyncio.gather(running, watching, return_exceptions=True)


async def try_handle_realtime(websocket, config, auth, *, active_devices=None, connection_factory=None, connection_lookup=None, credential_verifier=None):
    settings = config.get("codex_realtime", {})
    headers = dict(websocket.request.headers)
    device_id = headers.get("device-id")
    console_selection = settings.get("console_selection") is True and config.get("read_config_from_api") is True
    if settings.get("enabled") is not True or (not console_selection and device_id not in settings.get("device_ids", [])):
        return False
    token = headers.get("authorization", "")
    client_id = headers.get("client-id")
    native_candidate = token.startswith(("Bearer native1.", "Bearer native2."))
    verify_access = None
    if (config.get("server", {}).get("auth", {}).get("enabled") is not True
            or not device_id or not client_id or not token.startswith("Bearer ")
            or (not (console_selection and native_candidate)
                and not auth.verify_token(token[7:], client_id=client_id, username=device_id))):
        await websocket.close(code=1008, reason="Realtime device authentication required")
        return True
    if console_selection:
        try:
            selected = await (connection_lookup or lookup_console_connection)(config, device_id)
            if selected is None or selected.get("kind") == "api":
                if native_candidate:
                    await websocket.close(code=1008, reason="Native token cannot authorize the ordinary route")
                    return True
                return False
            if (selected.get("kind") != "chatgpt_realtime" or selected.get("enabled") is not True
                    or selected.get("runtimeId") != "local" or selected.get("model") != "gpt-6.1-terra"
                    or str(selected.get("userId")) not in {str(owner) for owner in settings.get("allowed_user_ids", [])}):
                await websocket.close(code=1008, reason="Realtime runtime access is not granted")
                return True
            if token.startswith("Bearer native2."):
                async def verify_access():
                    grant = await (credential_verifier or verify_console_device)(token[7:], device_id, client_id)
                    if (grant.get("valid") is not True or grant.get("connectionId") != selected["id"]
                            or str(grant.get("userId")) != str(selected["userId"])):
                        raise PermissionError("device grant no longer matches selected connection")
                await verify_access()
            elif not auth.verify_native_token(token[7:], client_id, device_id, selected["userId"], selected["id"]):
                await websocket.close(code=1008, reason="Owner-issued native device token required")
                return True
            from .account_profiles import AccountProfiles
            profiles = AccountProfiles(settings["account_profiles_dir"], Path(settings["workspace"]))
            profile = profiles.profile(selected["userId"], selected["id"])
            if not (profile / "auth.json").is_file():
                await websocket.close(code=1008, reason="ChatGPT connection requires sign-in")
                return True
            settings = {**settings, "codex_home": str(profile)}
            if settings.get('device_runtime',{}).get('enabled') is True:
                if verify_access is None:raise PermissionError('durable runtime requires revocable native2 device access')
                runtime=get_task_runtime(config)
                settings['conversation']=runtime.bind(selected['userId'],selected['id'],device_id,profile,verify_access)
        except Exception:
            await websocket.close(code=1013, reason="Service selection unavailable; reconnect required")
            return True
    active_devices = active_devices if active_devices is not None else set()
    active_key = device_id.lower()
    if active_key in active_devices:
        await websocket.close(code=1008, reason="Realtime device is already connected")
        return True
    active_devices.add(active_key)
    try:
        connection = (connection_factory or RealtimeConnection)(websocket, settings)
        if verify_access is None:
            await connection.run()
        else:
            await run_with_access_guard(connection, verify_access)
    except BufferError:
        await websocket.close(code=1013, reason="Realtime input buffer full; reconnect required")
    except PermissionError:
        await websocket.close(code=1008, reason="Device access revoked; reconnect requires authorization")
    except (ValueError, KeyError, TypeError):
        await websocket.close(code=1008, reason="Invalid realtime device configuration or message")
    except Exception as error:
        logging.getLogger(__name__).warning("Codex realtime connection failed (%s)", type(error).__name__)
        await websocket.close(code=1011, reason="Realtime connection failed; reconnect required")
    finally:
        active_devices.discard(active_key)
    return True


def get_task_runtime(config):
    settings=config['codex_realtime']
    if '_task_runtime' not in settings:
        from .task_runtime import TaskRuntime
        settings['_task_runtime']=TaskRuntime(settings)
    return settings['_task_runtime']


async def close_task_runtime(config):
    runtime=config.get('codex_realtime',{}).get('_task_runtime')
    if runtime is not None:await runtime.close()


def absolute_volume(text):
    """Recognize only a complete, single absolute-volume request; never execute it."""
    from .task_announcements import normalize_numbers
    text=normalize_numbers(text).strip()
    chinese=r'(?:请(?:帮我)?|帮我|麻烦你)?(?:把|将)?(?:设备的|扬声器的|外放的)?(?:音量|声音)(?:调到|调成|设置为|设置成|设为)(?:百分之)?(\d{1,3})(?:[%％])?[。.!！]?'
    english=r'(?:please )?(?:use the device tool to )?(?:set|change) the (?:speaker |device )?volume to (\d{1,3})(?: percent|%)?(?:\s*(?:[.;,]\s*|\s+)report the actual volume after reading it back)?[.!]?'
    match=re.fullmatch(chinese,text) or re.fullmatch(english,text)
    return int(match[1]) if match and 0<=int(match[1])<=100 else None


class RealtimeConnection:
    def __init__(self, websocket, settings, *, make_pipe=None):
        self.websocket = websocket
        self.settings = settings
        workspace = settings.get("workspace")
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise ValueError("realtime workspace must exist")
        self.workspace = Path(workspace).resolve()
        self.make_pipe = make_pipe
        self.session = None
        self.playback = None
        self.send_lock = asyncio.Lock()
        self.input_queue = asyncio.Queue()
        self.input_ready = False
        self.pending_bytes = 0
        self.pending_frames = 0
        self.pending_controls = 0
        self.input_generation = 0
        self.frame_ms = None
        self.conversation = settings.get('conversation')
        self.announcements = None
        self.voice_epoch = uuid4().hex
        self.backing_answers = {}
        self.device_mcp = None
        self.device_tools = None
        self.device_tool_config = None
        self.discovery = None
        self.started_at = time.monotonic()
        self.latency = []
        self.reply_started = None
        self.last_user_done = None
        self.device_control_pending = False
        self.device_control_deadline = 0
        self.verified_device_turns = set()
        self.device_summaries = {}
        self.device_volume_values = {}
        self.device_early_turns = set()
        self.absolute_volume_intent = None
        self.model_query_pending = False

    def measure(self,event,started,**fields):
        record={'voice_epoch':self.voice_epoch,'event':event,'time':time.time(),
                'elapsed_ms':round((time.monotonic()-started)*1000),**fields}
        self.latency.append(record);self.latency=self.latency[-32:]
        if not self.conversation:return
        import os
        import stat
        path=self.conversation.store.root/'connection-timings.jsonl'
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
            try:
                info=os.fstat(fd)
                if stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and info.st_nlink==1 and info.st_size<2097152:
                    os.write(fd,(json.dumps(record)+'\n').encode())
            finally:os.close(fd)
        except OSError:logging.getLogger(__name__).warning('Connection timing record unavailable')

    async def device_tool_request(self,method,params):
        from .device_mcp import ALLOWED
        if not self.conversation:raise PermissionError('device conversation required')
        await self.conversation.verify()
        if self.discovery:
            await asyncio.wait_for(asyncio.shield(self.discovery),10)
        names={name.removeprefix('self.').replace('.','_'):name for name in self.device_mcp.tools}
        if method=='tools/list':
            tools=[{'name':alias,'description':ALLOWED[name],'inputSchema':self.device_mcp.tools[name]['inputSchema']} for alias,name in names.items()]
            tools.append({'name':'model_info','description':'Read verified current backing model and actual background task execution models. Does not create a task or replay reports.',
                'inputSchema':{'type':'object','properties':{},'additionalProperties':False}})
            return {'tools':tools}
        if method!='tools/call' or not isinstance(params,dict):raise ValueError('unsupported tool request')
        args=params.get('arguments',{});alias=params.get('name')
        if self.last_user_done:self.measure('user_transcript_done_to_tool_dispatch',self.last_user_done,tool=alias if alias in names or alias=='model_info' else 'unsupported')
        if alias=='model_info':
            if args!={}:raise ValueError('model_info takes no arguments')
            sdk=self.session.pipe.media.session
            result={'voiceReady':self.input_ready,'threadId':getattr(sdk,'thread_id',None),'backingModel':getattr(sdk,'confirmed_model',None),'backingModelSource':'thread/start or thread/resume response',
                    'realtimeAcousticModel':None,'realtimeAcousticModelNote':'Codex native realtime service does not expose a verifiable acoustic model identifier.',
                    'workerConfiguredModel':'gpt-6.1-terra','tasks':[self.conversation.runtime.public(job) for job in self.conversation.store.tasks(self.conversation.scope)[-8:]],
                    'deviceTools':sorted(names),'deviceToolDiscoveryError':self.device_mcp.discovery_error}
        else:
            if alias not in names:raise ValueError('device tool unavailable')
            started=time.monotonic()
            if names[alias]=='self.audio_speaker.set_volume' and 'self.get_device_status' in self.device_mcp.tools:
                await self.device_mcp.call('self.get_device_status',{})
            result=await self.device_mcp.call(names[alias],args)
            self.measure('device_tool_acknowledged',started,tool=alias)
            # A successful write acknowledgement alone is not a measured volume.
            if names[alias]=='self.audio_speaker.set_volume' and 'self.get_device_status' in self.device_mcp.tools:
                readback=await self.device_mcp.call('self.get_device_status',{})
                values=[]
                for content in readback.get('content',[]):
                    try:
                        value=json.loads(content.get('text','')).get('audio_speaker',{}).get('volume')
                        if type(value) is int:values.append(value)
                    except (ValueError,AttributeError):pass
                measured=values[0] if len(values)==1 else None
                result={'writeAcknowledged':result,'readback':readback,'measuredVolume':measured,'matchesRequested':None if measured is None else measured==args['volume']}
                self.measure('volume_readback',started,requested=args['volume'],measured=measured)

        return {'content':[{'type':'text','text':json.dumps(result,ensure_ascii=False)}]}

    async def discover_device_tools(self):
        started=time.monotonic()
        await self.device_mcp.discover()
        self.measure('device_tools_discovered',started,tools=sorted(self.device_mcp.tools),discovery_error=self.device_mcp.discovery_error,
                     advertised_names=self.device_mcp.discovered_names)

    async def send(self, message):
        async with self.send_lock:
            await self.websocket.send(json.dumps(message) if isinstance(message, dict) else message)
        if isinstance(message,bytes) and self.reply_started is not None:
            self.measure('user_transcript_done_to_first_audio',self.reply_started);self.reply_started=None
        if isinstance(message,dict) and message.get('type')=='hello' and self.device_mcp and not self.device_mcp.discovery_error and self.discovery is None:
            self.discovery=asyncio.create_task(self.discover_device_tools())

    def _make_pipe(self, workspace, send_opus, **kwargs):
        factory = self.make_pipe
        if factory is None:
            from .device_media_pipe import DeviceMediaPipe
            factory = DeviceMediaPipe
        if self.settings.get("codex_home"):
            kwargs["codex_home"] = self.settings["codex_home"]
        pipe = factory(workspace, send_opus, **kwargs)
        self.playback = DevicePlayback(self.session, pipe)
        if self.conversation:
            from .task_runtime import TOOLS,VOICE_INSTRUCTIONS
            from .task_announcements import TaskAnnouncements
            from .codex_session import CodexRealtimeSession
            pipe.media.session=CodexRealtimeSession(self.workspace,codex_home=self.settings['codex_home'],
                conversation=self.conversation,tool_handler=self.conversation.tool_call,dynamic_tools=TOOLS,
                developer_instructions=VOICE_INSTRUCTIONS,device_tool_server=self.device_tool_config)
            self.announcements=TaskAnnouncements(self,self.conversation)
        pipe.on_pcm = self.announcements.feed if self.announcements else self.playback.feed
        return pipe

    async def _interrupt(self):
        # Do not use microphone energy to infer interruptions here. The device
        # explicitly aborted; new caller audio continues to the duplex service.
        self.playback.block()
        if self.announcements:self.announcements.interrupt()

    async def _start(self, hello, receiving):
        opening = asyncio.create_task(self.session.hello(
            hello, self.websocket.request.headers.get("protocol-version", "1"), uuid4().hex
        ))
        disconnected = asyncio.create_task(self.websocket.wait_closed())
        try:
            done, _ = await asyncio.wait({opening, disconnected, receiving}, timeout=60, return_when=asyncio.FIRST_COMPLETED)
            if receiving in done:
                receiving.result()
                raise ConnectionError("device stopped during realtime startup")
            if opening not in done:
                raise ConnectionError("device disconnected or realtime startup timed out")
            await opening
        finally:
            for task in (opening, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(opening, disconnected, return_exceptions=True)

    async def _receive(self):
        async for message in self.websocket:
            if isinstance(message, bytes):
                if len(message) > MAX_AUDIO_PACKET_BYTES + 16:
                    raise ValueError("oversized device audio frame")
                self.pending_frames += 1
                self.pending_bytes += len(message)
                if self.pending_frames * self.frame_ms > 10000 or self.pending_bytes > 262144:
                    raise BufferError("realtime input buffer full")
                self.input_queue.put_nowait(message)
            else:
                if len(message.encode()) > 16384:
                    raise ValueError("oversized device control message")
                event = json.loads(message)
                if not isinstance(event, dict):
                    raise ValueError("expected a device message")
                if event.get('type')=='mcp':
                    if self.device_mcp:self.device_mcp.receive(event)
                    continue
                if len(message.encode())>4096:raise ValueError('oversized device control message')
                if event.get("type") == "goodbye":
                    return
                if event.get("type") not in ("listen", "abort"):
                    raise ValueError("unsupported device message")
                if self.input_ready:
                    if event["type"] == "abort":
                        self._discard_input()
                        self.session.pipe.media.input.clear_audio()
                    await self.session.device_message(event)
                else:
                    self.pending_controls += 1
                    if self.pending_controls > 16:
                        raise ValueError("realtime control buffer full")
                    self.input_queue.put_nowait(event)

    def _discard_input(self):
        self.input_generation += 1
        while not self.input_queue.empty():
            self.input_queue.get_nowait()
        self.pending_bytes = self.pending_frames = self.pending_controls = 0

    async def _input(self):
        next_frame = None
        while True:
            message = await self.input_queue.get()
            if isinstance(message, bytes):
                generation = self.input_generation
                self.pending_frames -= 1
                self.pending_bytes -= len(message)
                now = asyncio.get_running_loop().time()
                if next_frame is None or now - next_frame > 0.1:
                    next_frame = now
                await asyncio.sleep(max(0, next_frame - now))
                if generation != self.input_generation:
                    next_frame = None
                    continue
                await self.session.device_audio(message)
                # Startup audio must not be burst-fed into the 600 ms PCM queue.
                next_frame += self.frame_ms / 1000
            else:
                self.pending_controls -= 1
                await self.session.device_message(message)

    def begin_user_turn(self,text):
        was_pending=self.device_control_pending
        self.absolute_volume_intent=absolute_volume(text)
        self.model_query_pending=bool(re.search(r'模型|型号|\bmodel\b',text,re.I))
        self.device_control_pending=bool(re.search(r'音量|(?:声音.{0,8}(?:大|小|调))|配网|闹钟|屏幕|主题|亮度|电量|\b(?:volume|louder|quieter|wifi|alarm|battery|theme|brightness)\b',text,re.I))
        self.device_control_deadline=time.monotonic()+15 if self.device_control_pending else 0
        if self.device_control_pending:self.playback.block()
        elif was_pending:self.playback.release()

    def remember_device_result(self,turn_id,item):
        if not turn_id:return
        self.verified_device_turns.add(turn_id)
        if len(self.verified_device_turns)>32:
            self.verified_device_turns={turn_id};self.device_summaries.clear();self.device_volume_values.clear();self.device_early_turns.clear()
        if item.get('tool')!='audio_speaker_set_volume':return
        for content in (item.get('result') or {}).get('content',[]):
            try:
                value=json.loads(content.get('text',''))
                measured=value.get('measuredVolume')
                if type(measured) is int and type(value.get('matchesRequested')) is bool:
                    if value['matchesRequested']:self.device_volume_values[turn_id]=measured
                    else:self.device_volume_values.pop(turn_id,None)
                    self.device_summaries[turn_id]=('音量'+str(measured)+'%。') if value['matchesRequested'] else ('设备读回音量为'+str(measured)+'%，与请求不一致，操作未确认成功。')
            except (ValueError,AttributeError,TypeError):continue

    def handle_device_result(self,turn_id,item):
        self.remember_device_result(turn_id,item)
        if item.get('tool')=='model_info' and self.model_query_pending and turn_id and turn_id not in self.device_early_turns:
            for content in (item.get('result') or {}).get('content',[]):
                try:
                    value=json.loads(content.get('text',''))
                    model=value.get('backingModel')
                    valid=lambda name:isinstance(name,str) and re.fullmatch(r'gpt-[0-9]+(?:\.[0-9]+)*-(?:terra|sol|astra|luna)',name)
                    if not valid(model):continue
                    models=sorted({job.get('execution',{}).get('model') for job in value.get('tasks',[]) if valid(job.get('execution',{}).get('model'))})
                    summary='对话后台'+model+'。'
                    summary+=('已核验任务执行模型与此相同。' if models==[model] else '已记录任务执行模型包括'+', '.join(models)+'。') if models else '暂无已核验的任务执行型号。'
                    if value.get('realtimeAcousticModel') is None:summary+='原声声学型号未公开。'
                    self.device_early_turns.add(turn_id)
                    self.announcements.reply(turn_id,summary)
                    break
                except (ValueError,AttributeError,TypeError):continue
        expected=self.absolute_volume_intent
        arguments=item.get('arguments',{})
        if isinstance(arguments,str):
            try:arguments=json.loads(arguments)
            except ValueError:return
        if not isinstance(arguments,dict):return
        if (self.device_control_pending and expected is not None and turn_id not in self.device_early_turns
                and item.get('tool')=='audio_speaker_set_volume'
                and arguments.get('volume')==expected
                and self.device_volume_values.get(turn_id)==expected):
            self.device_early_turns.add(turn_id)
            self.announcements.reply(turn_id,self.device_summaries[turn_id])
            if self.last_user_done:self.measure('verified_volume_reply_queued',self.last_user_done)

    def device_answer(self,turn_id,answer):
        if not self.device_control_pending:return answer
        if turn_id not in self.verified_device_turns:return '尚未收到本次设备操作的成功回执，不能确认已完成。'
        return self.device_summaries.get(turn_id,answer)

    async def _events(self, queue, *, app_server=False):
        while True:
            event = await queue.get()
            if not isinstance(event, dict):
                raise ValueError("invalid realtime event")
            kind = event.get("method" if app_server else "type", "")
            if kind in ("error", "thread/realtime/error", "thread/realtime/closed"):
                raise ConnectionError("realtime service ended")
            if app_server and self.announcements:
                params=event.get('params',{})
                sdk=self.session.pipe.media.session
                if params.get('threadId')==sdk.thread_id:
                    item=params.get('item',{})
                    if kind=='item/completed' and item.get('type')=='mcpToolCall' and item.get('server')=='xiaozhi_device' and item.get('status')=='completed':
                        self.handle_device_result(params.get('turnId'),item)
                    if kind=='item/completed' and params.get('item',{}).get('type')=='agentMessage' and params['item'].get('phase')!='commentary':
                        if len(self.backing_answers)>=8:raise BufferError('backing answer queue is full')
                        self.backing_answers[params.get('turnId')]=params['item'].get('text','')[:3000]
                    if kind=='turn/completed':
                        if self.last_user_done:self.measure('user_transcript_done_to_backing_done',self.last_user_done)
                        self.announcements.answer_ready()
                        turn=params.get('turn',{});ident=turn.get('id');answer=self.backing_answers.pop(ident,None)
                        failed=ident in sdk.tool_failures
                        if ident in self.device_early_turns:pass
                        elif failed:self.announcements.reply(ident,'操作未获确认，请稍后查询实际状态。')
                        elif ident not in sdk.control_turns and answer and turn.get('status')=='completed':self.announcements.reply(ident,self.device_answer(ident,answer))
                        sdk.control_turns.discard(ident);sdk.tool_failures.discard(ident)
            report=self.announcements.event(event) if self.announcements and not app_server else False
            suppress_unconfirmed=self.device_control_pending and event.get('turn',{}).get('role')=='assistant' and not report
            if self.conversation and not app_server and kind=='turn.done' and not report and not suppress_unconfirmed:
                turn=event.get('turn',{})
                self.conversation.remember(self.voice_epoch+str(turn.get('id')),turn.get('role'),turn.get('transcript'))
            if not app_server and kind == "turn.done" and event.get("turn", {}).get("role") == "user":
                self.reply_started=time.monotonic()
                self.last_user_done=self.reply_started
                text = event["turn"].get("transcript")
                if isinstance(text, str):
                    self.begin_user_turn(text)
                    if self.device_control_pending:await self.session.interrupt()
                    await self.send({"type": "stt", "text": text, "session_id": self.session.session_id})
            if self.conversation and not app_server and not report and not suppress_unconfirmed and kind=='turn.done' and event.get('turn',{}).get('role')=='assistant':
                text=event['turn'].get('transcript')
                if isinstance(text,str):
                    await self.send({'type':'tts','state':'sentence_start','text':text,'session_id':self.session.session_id})

    async def _watch(self):
        disconnected_at = None
        media = self.session.pipe.media
        while True:
            if not self.announcements or not self.announcements.current:
                await self.playback.finish_if_idle()
            if media.failure or media.session.event_overflow or getattr(media.peer, "connect_failure", None):
                raise ConnectionError("realtime transport failed")
            state = media.peer.connectionState
            if state in ("failed", "closed") or media.channel.readyState in ("closing", "closed"):
                raise ConnectionError("realtime connection closed")
            if state == "disconnected":
                disconnected_at = disconnected_at or asyncio.get_running_loop().time()
                if asyncio.get_running_loop().time() - disconnected_at >= 5:
                    raise ConnectionError("realtime connection lost")
            else:
                disconnected_at = None
            process = media.session.process
            if process is not None and process.returncode is not None:
                raise ConnectionError("Codex process exited")
            await asyncio.sleep(0.2)

    async def run(self):
        tasks = set()
        try:
            message = await asyncio.wait_for(self.websocket.recv(), 10)
            if not isinstance(message, str):
                raise ValueError("expected a JSON hello")
            hello = json.loads(message)
            if not isinstance(hello, dict):
                raise ValueError("expected a JSON hello")
            _, _, self.frame_ms = parse_hello(hello)
            if self.conversation:
                from .device_mcp import DeviceMcp
                from .device_tool_server import DeviceToolServer
                self.device_mcp=DeviceMcp(self.send,self.conversation.verify)
                if hello.get('features',{}).get('mcp') is not True:
                    self.device_mcp.discovery_error='Device hello does not advertise MCP.'
                self.device_tools=DeviceToolServer(self.device_tool_request)
                self.device_tool_config=await self.device_tools.start(self.conversation.store.root/('device-mcp-'+self.conversation.scope+'.json'))
            self.session = DeviceSession(self.workspace, self.send, self._interrupt,
                                         authenticated=True, make_pipe=self._make_pipe)
            receiving = asyncio.create_task(self._receive())
            tasks.add(receiving)
            await self._start(hello, receiving)
            sdk=self.session.pipe.media.session
            inventory=getattr(sdk,'device_inventory',None)
            self.measure('voice_ready',self.started_at,thread_tools=sorted(inventory['tools']) if inventory else [],confirmed_model=getattr(sdk,'confirmed_model',None))
            self.playback.ready = True
            self.input_ready = True
            media = self.session.pipe.media
            tasks.update({asyncio.create_task(self._input()), asyncio.create_task(self._events(media.events)),
                     asyncio.create_task(self._events(media.session.events, app_server=True)),
                     asyncio.create_task(self._watch())})
            if self.announcements:tasks.add(asyncio.create_task(self.announcements.run()))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            self.input_ready = False
            if self.playback is not None:
                self.playback.ready = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.device_mcp:self.device_mcp.close()
            if self.discovery:
                self.discovery.cancel();await asyncio.gather(self.discovery,return_exceptions=True)
            if self.device_tools:await self.device_tools.close()
            self.measure('connection_closed',self.started_at)
            if self.announcements:self.announcements.close()
            self._discard_input()
            if self.session is not None:
                await self.session.close()
