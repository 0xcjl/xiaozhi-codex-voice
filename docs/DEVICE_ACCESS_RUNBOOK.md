# 设备接入、凭据续期与回退

更新：2026-10-29，固定公网设备入口 device.example.invalid 已完成模拟联调，隔离控制台 server.ota 已衔接该域名；内部 WebSocket 仍由专用入口代理。官方账号、设备固件、设备地址、日常 Codex 登录不变。不是实体设备验收；公网配置与当前运行限制见 DEVICE_INGRESS.md。

## 兼容标准固件的认证

标准 v2.0.5 在启动时从 OTA 获取 WebSocket URL、token、version，保存到 NVS；唤醒时使用该凭据。因此不能要求其每 15 分钟自行刷新令牌。

设备所有者在“服务连接 → 设备 → 接入与回退”生成专属 OTA 地址。地址含 256 位随机凭据，只显示一次，数据库仅保存 SHA-256 摘要。OTA 返回同一设备凭据，不返回 ChatGPT OAuth 凭据、固件升级、MQTT 或其他模型回退。

凭据绑定设备归属、所选连接及修订、MAC 和客户端标识。客户端标识未知可留空，首次持有正确凭据和 MAC 的客户端会在数据库行锁下绑定；若已知标识，优先预先填写。专属地址必须保密；MAC 单独不是认证因素。

有效凭据采用 30 天滑动租约，连接时服务端续期；设备不需要新增刷新功能。超过 30 天未使用，所有者可在控制台续期同一凭据，无需重新配网。撤销、轮换、连接修订变化不能由旧凭据自我恢复。已经撤销的凭据必须重新生成，实际设备也要更新专属地址。

活动连接每 10 秒向管理端复核；单次管理请求限 4 秒。失败时停止语音并清理 WebRTC/Codex 资源，再关闭连接。关闭总时长还取决于底层进程清理，不能把“10 秒检查周期”承诺为所有情况下 10 秒完成关闭。控制台或数据库故障时拒绝连接，不回退到日常账号或普通模型。

## ChatGPT 订阅凭据

每条连接使用独立 CODEX_HOME，由 Codex 管理官方登录与自动刷新。控制台“刷新订阅凭据”调用 account/read(refreshToken=true)，不自行解析或传播 OAuth token。刷新失败可重试，或选择“重新官方授权”；新授权不先注销旧账号，不读取日常登录。重新授权的错误恢复分支由模拟测试覆盖，本轮未要求用户再次真实授权。

## 地址与设备资料

- 官方控制台的六位码属于账号绑定，不是自定义服务地址入口。
- 社区说明：原作者预编译固件 >=1.6.1 可在设备临时配网网页的高级选项设置 OTA。
- v2.0.5 + WiFi 组件 2.6.2：配网网页 http://192.168.4.1，GET /advanced/config 只读；POST /advanced/submit 才写入。本轮没有访问实体设备这些接口。
- 上游编译默认 OTA 为 https://api.tenclass.net/xiaozhi/ota/；设备保存值或厂商编译默认可能不同，不能用此值冒充已备份的实机原地址。
- 公开 WHA103 使用者报告 ESP32-C3 无 PSRAM；不是本机实测。公开上游板型未匹配 WHA103，未确认其配网按键。不套用其他板型的 BOOT/长按方法。
- 上游 AudioService 支持将服务端采样率重采样到硬件输出率；本轮语音采用 16kHz 单声道 Opus，厂商实际音频实现仍需实机验收。

## 实机切换前必须具备的材料

1. 确认对应厂商/板型的配网入口；不重置、不解绑。
2. 从设备只读页面记录实际原 OTA；若为空，另核实厂商编译默认并明确来源。
3. 记录 WiFi 恢复方式，保存专属新地址和原地址到安全位置。
4. 发布设备可达的 HTTPS OTA / WSS 入口，验证证书链与设备固件兼容。仅回环地址可用 HTTP/WS；不把管理 API、数据库或账号控制口暴露到局域网。
5. 入口代理不得记录 /ota/native/ 后的凭据，不允许把完整专属地址发送到第三方或日志平台。

这些条件未全部具备时，readiness 必须保持 readyToSwitch=false。固定网络入口已配置并通过模拟设备验证，但实体原地址与配网入口仍未知，不改设备地址。

## 三种回退不要混淆

### 恢复自建服务中的原模型

设备选择改为“沿用智能体原有模型配置”。只恢复自建配置，不会恢复设备官方 OTA，也不会修改官方账号绑定。此分支已用模拟设备验证。

### 撤销设备访问

控制台撤销专属凭据，新 OTA/语音请求立即被拒绝；活动连接下次复核后退出。不会自动切换其他模型，也不会注销 ChatGPT 日常账号。不要把“撤销”当作官方服务恢复。

### 恢复设备到官方服务

先进入已确认的配网入口，将 OTA 恢复为事先从实机核实并保存的原值，保存、重启并确认官方对话恢复。保留原有 xiaozhi.me 绑定，不执行解绑或恢复出厂。成功后再撤销自建凭据。实机原值未知时不执行本步骤；本轮仅验证模拟原地址的保存与读取。

若回退服务程序版本，应先确保设备已恢复原服务，再停止新语音入口并恢复此前程序和项目配置。新表可保留，不运行 DROP、清库或修改历史 Liquibase 变更；本轮未执行程序回滚。

## 隔离运行入口

控制台 http://127.0.0.1:18823/#/service-connections；Java 18821；独立订阅账号控制 18822；原生设备 WebSocket 18824；MySQL 13316；Redis 16389，全部回环监听。

原生设备入口位于 core/providers/realtime/device_server.py，仅提供回环监听和订阅路由，不支持公网 bind 参数。现有上游 WebSocket 服务也已接入相同路由判断；本轮没有为全量传统模型安装或启动额外供应商组件。

## 验收范围

本轮覆盖控制台配置、Shiro 归属权限、MySQL 持久化与并发绑定、OTA 发现、设备凭据、官方订阅刷新、真实 WebRTC/Opus 与资源释放。30 天过期通过移动隔离测试记录的 expires_at 验证，未等待 30 天。

后台任务与跨重连上下文恢复现已合入本分支，并在隔离主入口启用。配置与恢复语义见 [后台任务与重连运行说明](TASK_CONTEXT_RUNTIME.md)。这仍不是实体 ESP32、全天候稳定性或云节点验收。

## 一手资料

- https://github.com/xinnan-tech/xiaozhi-esp32-server/blob/1a095d5bed23e49a17f817c7f378e2a86f298dfc/docs/firmware-setting.md
- https://github.com/78/xiaozhi-esp32/blob/v2.0.5/main/Kconfig.projbuild
- https://github.com/78/xiaozhi-esp32/blob/v2.0.5/main/ota.cc
- https://github.com/78/xiaozhi-esp32/blob/v2.0.5/main/protocols/websocket_protocol.cc
- https://components.espressif.com/components/78/esp-wifi-connect/versions/2.6.2/readme
- https://github.com/espressif/esp-sr/issues/218
- https://learn.chatgpt.com/docs/app-server
