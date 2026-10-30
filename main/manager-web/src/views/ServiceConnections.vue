<template>
  <el-container class="connections-page">
    <HeaderBar />
    <main class="connections-content" v-loading="loading">
      <div class="page-heading">
        <div><h1>服务连接</h1><p>配置一次，再为设备选择。原有模型设置可随时恢复。</p></div>
        <el-button type="primary" @click="edit()">新增连接</el-button>
      </div>
      <el-alert :closable="false" type="info" title="ChatGPT 实时语音目前为实验接入。请先保存连接，再完成独立订阅授权。授权与日常 Codex 登录隔离；已授权不代表语音链路已验证。" />
      <p v-if="error" role="alert" class="error">{{ error }} <el-button type="text" @click="load">重新加载</el-button></p>
      <el-table :data="connections" empty-text="尚未配置服务连接" class="connection-table">
        <el-table-column prop="name" label="连接名称" />
        <el-table-column label="方式"><template slot-scope="s">{{ s.row.kind === 'api' ? 'API 模型' : 'ChatGPT 实时语音' }}</template></el-table-column>
        <el-table-column label="模型 / 节点"><template slot-scope="s">{{ s.row.kind === 'api' ? modelName(s.row.modelId) : 'local · ' + s.row.model }}</template></el-table-column>
        <el-table-column label="配置状态"><template slot-scope="s"><el-tag :type="s.row.enabled ? 'warning' : 'info'">{{ s.row.enabled ? '已启用' : '已停用' }}</el-tag></template></el-table-column>
        <el-table-column label="操作" width="160"><template slot-scope="s"><el-button type="text" @click="edit(s.row)">编辑</el-button><el-button v-if="s.row.kind === 'chatgpt_realtime'" type="text" @click="openAccount(s.row)">订阅授权</el-button></template></el-table-column>
      </el-table>
      <section class="device-section">
        <h2>设备使用哪条连接</h2><p>选择只在设备下次连接自建服务时生效，不会修改官方小智控制台。</p>
        <el-select v-model="deviceId" placeholder="选择自己的设备" @change="loadSelection" :disabled="loading || saving">
          <el-option v-for="d in devices" :key="d.id" :value="d.id" :label="d.name + ' · ' + d.macAddress" />
        </el-select>
        <el-select v-model="selectedId" placeholder="选择服务连接" :disabled="!deviceId || selectionLoading || saving">
          <el-option value="" label="沿用智能体原有模型配置" />
          <el-option v-for="c in connections" :key="c.id" :value="c.id" :label="c.name + (c.enabled ? '' : '（已停用）')" :disabled="!c.enabled" />
        </el-select>
        <el-button type="primary" :disabled="!deviceId || selectionLoading || loading" :loading="saving" @click="saveSelection">保存设备选择</el-button>
        <el-button :disabled="!deviceId || selectionLoading || saving" :loading="readinessBusy" @click="checkReadiness">检查设备接入</el-button>
        <el-button :disabled="!deviceId || selectionLoading || saving" @click="openAccess">接入与回退</el-button>
        <div v-if="readiness" class="readiness-panel">
          <h3>设备接入检查 · {{ readiness.device.name || readiness.device.id }}</h3>
          <p>本次只读检查不会切换地址、重置设备或刷固件。</p>
          <div v-for="item in readiness.checks" :key="item.code" class="readiness-item">
            <el-tag :type="item.status === 'passed' ? 'success' : 'warning'">{{ item.status === 'passed' ? '已核对' : '待完成' }}</el-tag>
            <strong>{{ item.label }}</strong><span>{{ item.detail }}</span>
          </div>
          <p v-if="!readiness.readyToSwitch">接入准备尚未完成，请勿据此修改实体设备。</p>
        </div>
        <p v-if="!devices.length && !loading">暂无已绑定设备。先在智能体管理中绑定设备。</p>
      </section>
      <el-dialog title="设备接入与回退" :visible.sync="accessDialog" width="min(600px, 94vw)" :close-on-click-modal="false" @closed="closeAccess">
        <div v-loading="accessBusy">
          <p>这里只准备自建服务的接入信息，不会改设备、官方控制台或固件。</p>
          <p>凭据状态：{{ accessStateLabel }}。有效连接会自动续期 30 天；长期离线过期后可在此续期，无需重新配网。撤销后必须重新生成地址。</p>
          <el-form label-position="top">
            <el-form-item label="客户端标识（未知可留空，首次携带专属凭据接入时绑定）"><el-input v-model="accessForm.clientId" maxlength="128" autocomplete="off" /></el-form-item>
            <el-form-item label="设备原 OTA 地址（从设备核实后填写；未知请留空）"><el-input v-model="accessForm.originalOtaUrl" maxlength="1024" autocomplete="off" /></el-form-item>
          </el-form>
          <el-alert v-if="accessError" :title="accessError" type="error" :closable="false" />
          <template v-if="accessResult.otaUrl">
            <p>专属接入地址仅本次显示，请保密保存。当前尚未执行设备切换。</p>
            <el-input :value="accessResult.otaUrl" readonly type="textarea" :rows="3" autocomplete="off" />
          </template>
          <h3>回退步骤</h3>
          <p v-if="!accessData.originalOtaUrl">尚未保存实机原地址，不能宣称已具备实机回退条件。上游默认值不等于当前设备保存值。</p>
          <template v-else><p>核实保存的原地址：{{ accessData.originalOtaUrl }}</p><p>进入设备配网页，将 OTA 地址恢复为该原值，保存并重启。确认官方连接恢复后，再撤销本控制台的设备凭据。原有官方账号绑定保留；不要解绑或恢复出厂。</p></template>
          <p>“沿用智能体原有模型配置”仅恢复自建服务内的模型选择，不会把设备地址切回官方。</p>
        </div>
        <span slot="footer">
          <el-button @click="accessDialog = false">关闭</el-button>
          <el-button :disabled="accessBusy || !['active','expired'].includes(accessData.state)" @click="accessAction('renew')">续期现有凭据</el-button>
          <el-button :disabled="accessBusy || accessData.state !== 'active'" type="danger" plain @click="accessAction('revoke')">撤销设备访问</el-button>
          <el-button type="primary" :disabled="accessBusy" @click="accessAction('provision')">生成 / 轮换接入地址</el-button>
        </span>
      </el-dialog>
      <el-dialog title="配置服务连接" :visible.sync="dialog" width="min(560px, 94vw)" :close-on-click-modal="false">
        <el-form label-position="top" @submit.native.prevent="save">
          <el-form-item label="连接名称"><el-input v-model="form.name" maxlength="80" autocomplete="off" /></el-form-item>
          <el-form-item label="接入方式"><el-select v-model="form.kind"><el-option value="api" label="已有 API 模型" /><el-option value="chatgpt_realtime" label="ChatGPT 实时语音（实验）" /></el-select></el-form-item>
          <template v-if="form.kind === 'api'">
            <el-form-item label="已配置的 LLM 模型"><el-select v-model="form.modelId" placeholder="选择已有模型"><el-option v-for="m in models" :key="m.id" :value="m.id" :label="m.modelName" /></el-select></el-form-item>
            <p>API 密钥统一保存在原有模型配置中。<router-link to="/model-config">前往模型配置</router-link></p>
          </template>
          <template v-else><p>运行节点：local（自建服务所在的 Mac 或云电脑）</p><p>执行模型：gpt-6.1-terra</p><p>保存后点击“订阅授权”，在 OpenAI 官方页面登录。每条连接使用独立凭据，不影响日常 Codex。云节点切换尚未开放。</p></template>
          <el-form-item label="允许设备选择"><el-switch v-model="form.enabled" /></el-form-item>
        </el-form>
        <span slot="footer"><el-button @click="dialog = false" :disabled="saving">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存配置</el-button></span>
      </el-dialog>
      <el-dialog :title="'ChatGPT 订阅授权 · ' + (accountTarget ? accountTarget.name : '')" :visible.sync="accountDialog"
        width="min(560px, 94vw)" :close-on-click-modal="false" @closed="closeAccount">
        <div v-loading="accountBusy">
          <p>在 OpenAI 官方页面完成登录。本控制台不会接收你的密码，也不会读取日常 Codex 的凭据。</p>
          <p role="status">{{ accountLabel }}</p>
          <p v-if="accountData.email">账号：{{ accountData.email }} <span v-if="accountData.planType">· {{ accountData.planType }}</span></p>
          <el-alert v-if="accountError" :title="accountError" type="error" :closable="false" />
          <template v-if="accountData.userCode">
            <p>打开下面的官方页面，输入一次性授权码：</p><p class="login-code">{{ accountData.userCode }}</p>
            <a :href="accountData.verificationUrl" target="_blank" rel="noopener noreferrer">打开 OpenAI 官方授权页面</a>
            <p>等待授权完成；此页面会自动更新。授权码只在当前窗口临时显示。</p>
          </template>
          <p v-if="accountData.state === 'signed_in'">订阅授权已保存。后续语音将使用这条连接的独立账号；语音连通性还需验证。</p>
        </div>
        <span slot="footer">
          <el-button @click="accountDialog = false">关闭</el-button>
          <el-button v-if="accountData.state === 'pending'" :disabled="accountBusy" @click="cancelAccount">取消本次授权</el-button>
          <el-button :disabled="accountBusy" @click="loadAccount">刷新状态</el-button>
          <el-button v-if="accountData.state === 'signed_in'" :disabled="accountBusy" @click="accountAction('/refresh')">刷新订阅凭据</el-button>
          <el-button v-if="accountData.state === 'signed_in' || accountError" :disabled="accountBusy" @click="accountAction('/reauthorize')">重新官方授权</el-button>
          <el-button v-if="accountData.state !== 'signed_in'" type="primary" :loading="accountBusy" @click="startAccount">{{ accountData.state === 'pending' ? '显示本次授权码' : '连接 ChatGPT' }}</el-button>
        </span>
      </el-dialog>
    </main>
  </el-container>
</template>
<script>
import HeaderBar from '@/components/HeaderBar.vue';
import modelApi from '@/apis/module/model';
import { connectionRequest, connectionBody } from '@/apis/module/serviceConnection';
const emptyForm = () => ({ name: '', kind: 'api', modelId: '', enabled: false, revision: 0 });
export default {
  name: 'ServiceConnections', components: { HeaderBar },
  data: () => ({ connections: [], devices: [], models: [], loading: false, saving: false,
    accessDialog: false, accessDeviceId: '', accessData: {}, accessForm: { clientId: '', originalOtaUrl: '' }, accessResult: {}, accessError: '', accessBusy: false, accessVersion: 0,
    error: '', dialog: false, form: emptyForm(), editingId: null, deviceId: '', selectedId: '', selectionLoading: false, selectionVersion: 0, readiness: null, readinessBusy: false, readinessVersion: 0, accountDialog: false, accountTarget: null, accountData: {}, accountBusy: false, accountError: '', accountPoll: null, accountVersion: 0 }),
  created() { this.load(); },
  beforeDestroy() { this.selectionVersion++; this.readinessVersion++; this.closeAccount(); this.closeAccess(); },
  computed: {
    accessStateLabel() { return ({ active: '有效', expired: '已过期', revoked: '已撤销', not_configured: '尚未生成' })[this.accessData.state] || '正在读取'; },
    accountLabel() {
      return ({ signed_out: '尚未登录', signed_in: '已授权 · 语音连通性待确认', pending: '等待你在官方页面完成授权',
        expired: '本次授权已超时，请重新连接', cancelled: '本次授权已取消', failed: '授权未完成，请重试' })[this.accountData.state] || '正在查询授权状态';
    },
  },
  methods: {
    closeAccess() { this.accessVersion++; this.accessResult = {}; this.accessData = {}; this.accessError = ''; this.accessBusy = false; },
    async openAccess() {
      this.closeAccess(); this.accessDeviceId = this.deviceId; this.accessDialog = true;
      this.accessForm = { clientId: '', originalOtaUrl: '' };
      await this.accessAction();
      if (this.accessDialog) this.accessForm = { clientId: this.accessData.clientId || '', originalOtaUrl: this.accessData.originalOtaUrl || '' };
    },
    async accessAction(action = '') {
      if (!this.accessDialog || !this.accessDeviceId || this.accessBusy) return;
      const deviceId = this.accessDeviceId; const initialVersion = this.accessVersion;
      if ((action === 'provision' && this.accessData.state !== 'not_configured') || action === 'revoke') {
        try { await this.$confirm(action === 'revoke' ? '撤销后新连接立即被拒绝，活动语音在下一次权限检查时关闭。设备地址不会自动恢复。' : '重新生成会使旧接入地址失效；设备需要保存新地址。', '确认设备凭据操作', { type: 'warning' }); }
        catch (_) { return; }
        if (initialVersion !== this.accessVersion || !this.accessDialog) return;
      }
      const version = ++this.accessVersion; this.accessBusy = true; this.accessError = ''; this.accessResult = {};
      const path = '/devices/' + encodeURIComponent(deviceId) + '/access';
      try {
        const result = await connectionRequest(path + (action && action !== 'provision' ? '/' + action : ''), action ? 'POST' : 'GET', action === 'provision' ? { ...this.accessForm } : undefined);
        if (version !== this.accessVersion || !this.accessDialog) return;
        if (action === 'provision') this.accessResult = result;
        const status = action ? await connectionRequest(path) : result;
        if (version === this.accessVersion && this.accessDialog) this.accessData = status;
      } catch (e) { if (version === this.accessVersion) this.accessError = e.message; }
      finally { if (version === this.accessVersion) this.accessBusy = false; }
    },
    async checkReadiness() {
      if (!this.deviceId) return;
      const version = ++this.readinessVersion; const deviceId = this.deviceId;
      this.readiness = null; this.readinessBusy = true;
      try {
        const result = await connectionRequest('/devices/' + encodeURIComponent(deviceId) + '/readiness');
        if (version === this.readinessVersion && deviceId === this.deviceId) this.readiness = result;
      } catch (e) { if (version === this.readinessVersion) this.$message.error(e.message); }
      finally { if (version === this.readinessVersion) this.readinessBusy = false; }
    },
    openAccount(row) {
      this.closeAccount(); this.accountTarget = { id: row.id, name: row.name }; this.accountDialog = true;
      return this.loadAccount();
    },
    closeAccount() {
      clearTimeout(this.accountPoll); this.accountPoll = null; this.accountVersion++;
      this.accountData = {}; this.accountError = ''; this.accountBusy = false;
    },
    async accountAction(action = '') {
      if (!this.accountTarget || !this.accountDialog) return;
      const version = ++this.accountVersion;
      clearTimeout(this.accountPoll); this.accountPoll = null; this.accountBusy = true; this.accountError = '';
      const path = '/' + encodeURIComponent(this.accountTarget.id) + '/account' + action;
      try {
        const result = await connectionRequest(path, action ? 'POST' : 'GET');
        if (version !== this.accountVersion || !this.accountDialog) return;
        if (result.state === 'pending' && !result.userCode && this.accountData.userCode) {
          this.accountData = { ...this.accountData, ...result };
        } else this.accountData = result;
        if (result.state === 'pending') this.accountPoll = setTimeout(() => this.loadAccount(), 3000);
      } catch (e) { if (version === this.accountVersion) this.accountError = e.message; }
      finally { if (version === this.accountVersion) this.accountBusy = false; }
    },
    loadAccount() { return this.accountAction(); },
    startAccount() { return this.accountAction('/login'); },
    cancelAccount() { return this.accountAction('/cancel'); },
    modelName(id) { return this.models.find(m => m.id === id)?.modelName || id; },
    async load() {
      this.loading = true; this.error = '';
      try {
        const [connections, devices, models] = await Promise.all([
          connectionRequest(), connectionRequest('/devices'),
          new Promise((resolve, reject) => modelApi.getModelNames('LLM', '', res => resolve(res.data.data || []), reject)),
        ]);
        this.connections = connections; this.devices = devices; this.models = models;
      } catch (_) { this.error = '无法加载服务连接，请检查控制台服务和登录状态。'; }
      finally { this.loading = false; }
    },
    edit(item) { this.editingId = item?.id || null; this.form = item ? { ...item } : emptyForm(); this.dialog = true; },
    async save() {
      if (!this.form.name.trim() || (this.form.kind === 'api' && !this.form.modelId)) { this.$message.error('请填写名称并选择模型'); return; }
      this.saving = true;
      try {
        await connectionRequest(this.editingId ? '/' + encodeURIComponent(this.editingId) : '', this.editingId ? 'PUT' : 'POST', connectionBody(this.form));
        this.dialog = false; this.$message.success('配置已保存；连通性尚未确认'); await this.load();
      } catch (e) { this.$message.error(e.message); }
      finally { this.saving = false; }
    },
    async loadSelection() {
      this.accessDialog = false; this.closeAccess();
      const version = ++this.selectionVersion;
      this.readinessVersion++; this.readiness = null; this.readinessBusy = false;
      this.selectionLoading = true; this.selectedId = '';
      try {
        const result = await connectionRequest('/devices/' + encodeURIComponent(this.deviceId));
        if (version === this.selectionVersion) this.selectedId = result.connectionId;
      } catch (e) { if (version === this.selectionVersion) { this.deviceId = ''; this.$message.error(e.message); } }
      finally { if (version === this.selectionVersion) this.selectionLoading = false; }
    },
    async saveSelection() {
      this.readinessVersion++; this.readiness = null; this.readinessBusy = false;
      this.saving = true;
      try { await connectionRequest('/devices/' + encodeURIComponent(this.deviceId), 'PUT', { connectionId: this.selectedId }); this.$message.success('设备选择已保存，下次连接生效'); }
      catch (e) { this.$message.error(e.message); }
      finally { this.saving = false; }
    },
  },
};
</script>
<style scoped>
.connections-page { min-height: 100vh; flex-direction: column; background: #f5f7fb; }
.connections-content { width: min(1160px, calc(100% - 48px)); margin: 36px auto; }
.page-heading { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; gap: 20px; }
h1 { font-size: 26px; margin: 0 0 10px; color: #243247; } h2 { font-size: 20px; color: #243247; }
p { color: #64748b; line-height: 1.7; } .error { color: #b42318; }
.login-code { font: 600 26px ui-monospace, monospace; letter-spacing: 3px; color: #243247; user-select: all; }
.connection-table { margin-top: 24px; border-radius: 12px; }
.device-section { margin-top: 28px; padding: 24px; background: white; border-radius: 12px; }
.readiness-panel { margin-top: 22px; padding-top: 8px; border-top: 1px solid #e2e8f0; }
.readiness-item { display: flex; flex-wrap: wrap; gap: 10px; align-items: baseline; margin: 14px 0; color: #64748b; }
.device-section .el-select { margin: 0 12px 12px 0; width: 280px; max-width: 100%; }
@media (max-width: 600px) { .connections-content { width: calc(100% - 24px); margin-top: 20px; } .page-heading { align-items: flex-start; } }
</style>
