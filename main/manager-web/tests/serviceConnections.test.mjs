import Vue from 'vue';
import { parseComponent } from 'vue-template-compiler';
import { describe, expect, it, vi } from 'vitest';
import source from '../src/views/ServiceConnections.vue?raw';
import apiSource from '../src/apis/module/serviceConnection.js?raw';
const apiScript = apiSource.replace(/^import .*;$/gm, '').replaceAll('export function ', 'function ');
const body = new Function(apiScript + '; return connectionBody;')();
const script = parseComponent(source).script.content.replace(/^import .*;$/gm, '').replace('export default', 'return');
function makeVm(request) {
  const component = new Function('HeaderBar', 'modelApi', 'connectionRequest', 'connectionBody', script)({}, {}, request, body);
  const vm = new Vue({ ...component, created: undefined });
  vm.$message = { error: vi.fn(), success: vi.fn() };
  return vm;
}
describe('service connection configuration', () => {
  it('drops stale type-specific fields and never forwards copied provider secrets', () => {
    expect(body({ name: ' voice ', kind: 'chatgpt_realtime', revision: 2, enabled: true, modelId: 'old', apiKey: 'secret' }))
      .toEqual({ name: 'voice', kind: 'chatgpt_realtime', revision: 2, enabled: true, modelId: null, runtimeId: 'local', model: 'gpt-6.1-terra' });
  });
  it('does not let a slow device response overwrite a newer selection', async () => {
    let oldResolve;
    const request = vi.fn().mockImplementationOnce(() => new Promise(r => { oldResolve = r; }))
      .mockResolvedValueOnce({ connectionId: 'new' });
    const vm = makeVm(request);
    vm.deviceId = 'old-device'; const old = vm.loadSelection();
    vm.deviceId = 'new-device'; await vm.loadSelection();
    oldResolve({ connectionId: 'old' }); await old;
    expect(vm.selectedId).toBe('new'); expect(vm.selectionLoading).toBe(false);
  });
  it('a failed selection read cannot clear the previous binding accidentally', async () => {
    const vm = makeVm(vi.fn().mockRejectedValue(new Error('offline')));
    vm.deviceId = 'device'; await vm.loadSelection();
    expect(vm.deviceId).toBe(''); expect(vm.$message.error).toHaveBeenCalled();
  });
  it('does not claim saved after a failed mutation', async () => {
    const request = vi.fn().mockRejectedValue(new Error('conflict'));
    const vm = makeVm(request); vm.form = { name: 'API', kind: 'api', modelId: 'm', revision: 1, enabled: true }; vm.dialog = true;
    await vm.save(); expect(vm.dialog).toBe(true); expect(vm.saving).toBe(false);
    expect(vm.$message.success).not.toHaveBeenCalled(); expect(request).toHaveBeenCalledTimes(1);
  });
});

describe('subscription authorization dialog', () => {
  it('never applies account status from a previously selected connection', async () => {
    let oldResolve;
    const request = vi.fn().mockImplementationOnce(() => new Promise(r => { oldResolve = r; }))
      .mockResolvedValueOnce({ state: 'signed_in', email: 'new@example.invalid' });
    const vm = makeVm(request);
    const old = vm.openAccount({ id: 'old', name: 'old' });
    await vm.openAccount({ id: 'new', name: 'new' });
    oldResolve({ state: 'signed_in', email: 'old@example.invalid' }); await old;
    expect(vm.accountData.email).toBe('new@example.invalid'); vm.$destroy();
  });
  it('does not retry failed login mutations or retain a code after close', async () => {
    const request = vi.fn().mockRejectedValue(new Error('unavailable'));
    const vm = makeVm(request); vm.accountTarget = { id: 'connection' }; vm.accountDialog = true;
    await vm.startAccount(); expect(request).toHaveBeenCalledTimes(1);
    expect(vm.accountError).toBe('unavailable'); expect(vm.accountBusy).toBe(false);
    vm.accountData = { state: 'pending', userCode: 'ABCD-1234' }; vm.closeAccount();
    expect(vm.accountData).toEqual({}); expect(vm.accountPoll).toBe(null); vm.$destroy();
  });
  it('cancel clears the one-time code and stops polling', async () => {
    const vm = makeVm(vi.fn().mockResolvedValue({ state: 'cancelled' }));
    vm.accountTarget = { id: 'connection' }; vm.accountDialog = true;
    vm.accountData = { state: 'pending', userCode: 'ABCD-1234' };
    await vm.cancelAccount(); expect(vm.accountData).toEqual({ state: 'cancelled' });
    expect(vm.accountPoll).toBe(null); vm.$destroy();
  });
});

describe('read-only device readiness', () => {
  it('does not attach a stale readiness result to another selected device', async () => {
    let finish;
    const vm = makeVm(vi.fn().mockImplementation(() => new Promise(r => { finish=r; })));
    vm.deviceId='first'; const pending=vm.checkReadiness();
    vm.deviceId='second'; finish({device:{id:'first'},checks:[],readyToSwitch:false}); await pending;
    expect(vm.readiness).toBe(null); vm.$destroy();
  });
  it('uses a read-only GET and preserves blocked status', async () => {
    const result={device:{id:'device'},checks:[{code:'ingress',status:'blocked'}],readyToSwitch:false,configurationChanged:false};
    const request=vi.fn().mockResolvedValue(result); const vm=makeVm(request);vm.deviceId='device';
    await vm.checkReadiness();expect(request).toHaveBeenCalledWith('/devices/device/readiness');
    expect(vm.readiness.readyToSwitch).toBe(false);expect(vm.readiness.configurationChanged).toBe(false);vm.$destroy();
  });
});

describe('device credential lifecycle', () => {
  it('opening only reads status and never provisions automatically', async () => {
    const request=vi.fn().mockResolvedValue({state:'not_configured'});const vm=makeVm(request);vm.deviceId='device';
    await vm.openAccess();expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith('/devices/device/access','GET',undefined);
    expect(vm.accessForm.originalOtaUrl).toBe('');vm.$destroy();
  });
  it('closing clears the one-time address and ignores a late secret response', async () => {
    let resolve;const request=vi.fn().mockImplementation(()=>new Promise(r=>{resolve=r;}));
    const vm=makeVm(request);vm.accessDialog=true;vm.accessDeviceId='device';vm.accessData={state:'not_configured'};
    const pending=vm.accessAction('provision');vm.accessDialog=false;vm.closeAccess();
    resolve({otaUrl:'https://test.invalid/private'});await pending;
    expect(vm.accessResult).toEqual({});expect(request).toHaveBeenCalledTimes(1);vm.$destroy();
  });
  it('rotation cannot happen after confirmation is cancelled', async () => {
    const request=vi.fn();const vm=makeVm(request);vm.accessDialog=true;vm.accessDeviceId='device';vm.accessData={state:'active'};
    vm.$confirm=vi.fn().mockRejectedValue('cancel');await vm.accessAction('provision');expect(request).not.toHaveBeenCalled();vm.$destroy();
  });
  it('renewal sends no device address or subscription credentials', async () => {
    const request=vi.fn().mockResolvedValue({state:'active'});const vm=makeVm(request);
    vm.accessDialog=true;vm.accessDeviceId='device';await vm.accessAction('renew');
    expect(request.mock.calls[0]).toEqual(['/devices/device/access/renew','POST',undefined]);vm.$destroy();
  });
});
