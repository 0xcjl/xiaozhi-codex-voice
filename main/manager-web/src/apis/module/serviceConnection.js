import { getServiceUrl } from '../api';
import RequestService from '../httpRequest';

// No automatic retries for mutations; a timeout may have committed on the server.
export function connectionRequest(path = '', method = 'GET', data = {}) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('请求超时，请刷新核对保存状态')), 35000);
    const fail = (error) => {
      clearTimeout(timer);
      reject(new Error(error?.data?.msg || '请求失败，请刷新后重试'));
    };
    RequestService.sendRequest().url(`${getServiceUrl()}/service-connections${path}`)
      .method(method).data(data)
      .success(res => { clearTimeout(timer); resolve(res.data.data); })
      .fail(fail).networkFail(fail).send();
  });
}

export function connectionBody(form) {
  return {
    name: form.name.trim(), kind: form.kind, enabled: form.enabled,
    revision: form.revision,
    modelId: form.kind === 'api' ? form.modelId : null,
    runtimeId: form.kind === 'chatgpt_realtime' ? 'local' : null,
    model: form.kind === 'chatgpt_realtime' ? 'gpt-6.1-terra' : null,
  };
}
