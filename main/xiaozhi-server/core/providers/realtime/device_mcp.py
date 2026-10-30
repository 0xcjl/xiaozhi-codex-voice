"""Connection-scoped device MCP. Firmware, reboot and asset URL tools stay blocked."""
import asyncio
import json
import time

ALLOWED = {
    'self.get_device_status': 'Read the actual device speaker volume, battery and status.',
    'self.audio_speaker.set_volume': 'Set actual speaker volume, integer 0 to 100, only when requested.',
    'self.screen.set_brightness': 'Set display brightness, integer 0 to 100, only when requested.',
    'self.screen.set_theme': 'Change the device display theme when requested.',
    'self.set_alarm': 'Set a device alarm only when explicitly requested.',
    'self.delete_alarm': 'Delete a specific device alarm only when explicitly requested.',
    'self.get_alarms': 'Read device alarms.',
    'self.start_wifi_config': 'Enter WiFi provisioning ONLY on explicit user request. Disconnects the device; never reset or unbind.',
    'self.get_system_info': 'Read device hardware and firmware information; not the cloud model.',
    'self.screen.get_info': 'Read device screen information.',
}


def clean_schema(schema):
    properties=schema.get('properties',{})
    if not isinstance(properties,dict) or len(properties)>12:raise ValueError('invalid device schema')
    result={'type':'object','properties':{},'required':schema.get('required',[]),'additionalProperties':False}
    if not isinstance(result['required'],list) or any(not isinstance(key,str) or key not in properties for key in result['required']):raise ValueError('invalid required arguments')
    for key,rule in properties.items():
        if not isinstance(key,str) or len(key)>64 or not isinstance(rule,dict) or rule.get('type') not in ('string','integer','boolean'):raise ValueError('unsupported device schema')
        item={field:rule[field] for field in ('type','minimum','maximum','enum') if field in rule}
        if item['type']=='string':item['maxLength']=1024
        result['properties'][key]=item
    return result


def validate(schema, arguments):
    if not isinstance(arguments, dict):raise ValueError('expected object arguments')
    properties=schema.get('properties', {})
    if not isinstance(properties,dict) or set(arguments)-set(properties):raise ValueError('unknown arguments')
    if set(schema.get('required',[]))-set(arguments):raise ValueError('missing arguments')
    for key,value in arguments.items():
        rule=properties[key];kind=rule.get('type')
        if kind=='integer':valid=type(value) is int
        elif kind=='boolean':valid=type(value) is bool
        elif kind=='string':valid=isinstance(value,str) and len(value)<=1024
        else:valid=False
        if not valid:raise ValueError('unsupported argument type')
        if 'enum' in rule and value not in rule['enum']:raise ValueError('unsupported argument value')
        if kind=='integer' and (value<rule.get('minimum',-2147483648) or value>rule.get('maximum',2147483647)):
            raise ValueError('argument outside device limits')


class DeviceMcp:
    def __init__(self,send,verify,*,timeout=8):
        self.send,self.verify,self.timeout=send,verify,timeout
        self.serial=0;self.pending={};self.tools={};self.closed=False
        self.timings=[];self.discovery_error=None;self.discovered_names=[]

    def receive(self,event):
        payload=event.get('payload')
        if not isinstance(payload,dict) or payload.get('jsonrpc')!='2.0':raise ValueError('invalid MCP response')
        ident=payload.get('id')
        if type(ident) is not int:return
        future=self.pending.get(ident)
        if future and not future.done():future.set_result(payload)

    async def request(self,method,params):
        if self.closed:raise ConnectionError('device disconnected')
        if len(self.pending)>=8:raise BufferError('device tool capacity reached')
        self.serial+=1;ident=self.serial;future=asyncio.get_running_loop().create_future()
        self.pending[ident]=future;started=time.monotonic()
        try:
            await self.send({'type':'mcp','payload':{'jsonrpc':'2.0','id':ident,'method':method,'params':params}})
            response=await asyncio.wait_for(future,self.timeout)
            if 'error' in response:raise ValueError('device rejected MCP request')
            result=response.get('result')
            if not isinstance(result,dict) or result.get('isError') is True:raise ValueError('device tool failed')
            return result
        finally:
            self.pending.pop(ident,None)
            self.timings.append({'method':method,'elapsed_ms':round((time.monotonic()-started)*1000)})
            self.timings=self.timings[-32:]

    async def discover(self):
        try:
            await self.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'xiaozhi-native-device','version':'1'}})
            cursor='';seen=set();found={}
            for _ in range(8):
                result=await self.request('tools/list',{'cursor':cursor,'withUserTools':True})
                tools=result.get('tools',[])
                if not isinstance(tools,list) or len(tools)>64:raise ValueError('invalid device tools')
                for item in tools:
                    if not isinstance(item,dict):raise ValueError('invalid device tool')
                    name=item.get('name');schema=item.get('inputSchema',{})
                    if isinstance(name,str):self.discovered_names.append(name[:128])
                    if name in ALLOWED and isinstance(schema,dict):
                        try:found[name]={'inputSchema':clean_schema(schema)}
                        except ValueError:continue
                cursor=result.get('nextCursor','')
                if not cursor:break
                if not isinstance(cursor,str) or len(cursor)>256 or cursor in seen:raise ValueError('invalid device tool cursor')
                seen.add(cursor)
            else:raise ValueError('device tool pagination limit')
            self.tools=found
        except (TimeoutError,ConnectionError,ValueError) as error:
            self.discovery_error=type(error).__name__+': '+'Device tool discovery unavailable; do not claim control succeeded.'

    async def call(self,name,arguments):
        if name not in ALLOWED or name not in self.tools:raise ValueError('device tool unavailable or prohibited')
        validate(self.tools[name]['inputSchema'],arguments)
        if name=='self.audio_speaker.set_volume' and (set(arguments)!={'volume'} or type(arguments['volume']) is not int or not 0<=arguments['volume']<=100):
            raise ValueError('volume must be an integer 0..100')
        await self.verify()
        return await self.request('tools/call',{'name':name,'arguments':arguments})

    def close(self):
        self.closed=True
        for future in self.pending.values():
            if not future.done():future.set_exception(ConnectionError('device disconnected'))
