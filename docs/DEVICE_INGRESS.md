# 设备专用入口

状态：2026-10-29 已配置固定设备入口 https://device.example.invalid 与独立命名 Tunnel example-device-tunnel。默认系统 DNS 下五次不同合成短句的原生语音全部通过，启停网关和连接器后另一次 OTA/语音通过；入口保留运行。未安装系统服务或开机启动，尚未连接实体 ESP32。原临时 Quick Tunnel 已关闭；其 DNS 限制不适用于本轮固定域名的实测结果。完整证据见工作区 outputs/esp32-codex-bridge-preparation/固定域名与独立Tunnel联调报告.md。

## 路由范围

`core/providers/realtime/device_ingress.py` 是 HTTPS/WSS 边缘服务的回环源站，仅接受：

- `POST /xiaozhi/ota/native/<设备专属凭据>`：转到回环 Java native OTA 接口。鉴权由现有设备授权服务执行，返回值只允许 websocket 字段；把内部 WS 地址改为配置的公网 WSS 地址。
- `GET /xiaozhi/v1/` 的 WebSocket 升级：只转到回环原生设备服务。仅转发 Device-Id、Client-Id、Protocol-Version 和 native2 Authorization。

控制台、订阅授权、管理配置、普通 OTA、登录等路径均不开放。请求 Cookie、转发来源头、代理认证不会向上游传播；没有任意目标代理、跨设备 cookie 存储或自动重定向。拒绝响应不回显上游数据或凭据。访问日志禁用，不记录包含专属令牌的 URL。

并行入口最多 8 个；HTTP 设备报告和单个 WS 消息最多 16 KiB；OTA 响应最多 8 KiB。OTA 先鉴权再有界丢弃设备报告，不向管理端转发无关报告内容。上游 HTTP/升级有等待期限，成功建立的 WS 不因这项 HTTP 读取期限被当作短请求关闭。

## 本机检查

从 main/xiaozhi-server 运行项目虚拟环境中的 Python：

```sh
python -m core.providers.realtime.device_ingress --public-origin https://device.example.invalid --port 18825
```

示例域名只是占位符。此命令仅绑定 127.0.0.1；不会创建隧道、签发设备凭据、更改数据库参数或改变实体配置。占位符不能写入实体设备。正常回环原生服务仍是 18824，管理服务仍是 18821。

自动化包含路由拒绝、鉴权、Cookie 隔离、OTA 与 WS 重定向阻断、固件/错误凭据过滤、容量与帧大小、撤销关闭码以及真实本机 native2 OTA 和原生音频。测试不是公网证书或实机验收。

## 下一阶段的加密公网验证

本机已有 cloudflared，不需要安装。建议先使用临时 Quick Tunnel，只用模拟设备做 HTTPS/WSS 验证，结束即停止。不新增系统服务、不改现有 tunnel 配置，不运行 cloudflared login，不使用已有账号证书或其他隧道凭据。使用项目独立空配置和专门的进程生命周期。

允许转发的数据仅为：模拟设备标识、设备专属 native2 凭据、合成音频与对应回复。Cloudflare 会终止边缘 TLS 并转发这些内容；ChatGPT OAuth 和 Codex 登录目录不通过该入口。没有登录页或管理 API 对公网开放。该步骤会新增外部网络访问，须获得单独授权后才执行。

[Cloudflare Quick Tunnel 官方说明](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) 指出：地址临时生成，停止进程即失效，适合开发测试，不承诺可用性。后续实机常用必须选择稳定域名/固定服务；不把临时域名作为设备的长期配置。邮件交互验证不适用于无浏览器的设备，设备继续使用 native2 凭据，控制台的人类登录与设备认证分开。

## 实体设备仍待核实

没有找到 WHA103 厂商的可靠配网按键说明，不能用其他板型的长按/BOOT 方法代替。官方六位验证码是账号绑定，不是自定义服务器入口。

上游 v2.0.5 从 WiFi NVS 的 ota_url 读取地址，空值才使用编译默认；WiFi 2.6.2 配网页的 GET /advanced/config 是读取，POST /advanced/submit 才写入。必须从实际设备核实并保存原地址及厂商配网方式，不能把上游默认地址当成已备份的实机值。本阶段没有访问这些设备接口。

公网验证通过后，仍需确认设备信任证书链、实际板型的音频行为和原地址回退材料，才可另行执行实体切换。订阅、任务队列、上下文和报告缓存继续由 Mac 承担，ESP32 只处理音频帧。readyToSwitch 保持 false。
