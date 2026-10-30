package xiaozhi.modules.connection.service;

import java.time.Instant;
import java.util.*;
import lombok.AllArgsConstructor;
import org.springframework.stereotype.Service;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.sys.service.SysParamsService;

@Service
@AllArgsConstructor
public class DeviceAccessService {
    private final ServiceConnectionService connections;
    private final AccountAuthorizationService accounts;
    private final SysParamsService params;

    public Map<String, Object> readiness(String deviceId, Long owner) {
        var device = connections.deviceInfo(deviceId, owner);
        var selection = connections.selection(deviceId, owner);
        List<Map<String,String>> checks = new ArrayList<>();
        checks.add(check("ownership", "设备归属", "passed", "已确认属于当前控制台用户"));
        String id = selection.get("connectionId");
        if (id == null || id.isBlank()) {
            checks.add(check("selection", "订阅语音连接", "blocked", "尚未为此设备保存订阅语音连接"));
        } else {
            var connection = connections.owned(id, owner);
            boolean nativeVoice = "chatgpt_realtime".equals(connection.getKind()) && Boolean.TRUE.equals(connection.getEnabled());
            checks.add(check("selection", "订阅语音连接", nativeVoice ? "passed" : "blocked", nativeVoice ? "已选择启用的独立订阅连接" : "所选连接未启用或不是订阅语音"));
            if (nativeVoice) {
                try {
                    boolean signedIn = "signed_in".equals(accounts.account(id,owner,"status").get("state"));
                    checks.add(check("account", "独立订阅授权", signedIn ? "passed" : "blocked", signedIn ? "专用账号已授权" : "请先在服务连接中完成订阅授权"));
                } catch (RenException e) {
                    checks.add(check("account", "独立订阅授权", "blocked", e.getMsg()));
                }
            }
        }
        boolean authEnabled="true".equalsIgnoreCase(params.getValue(Constant.SERVER_AUTH_ENABLED,true));
        checks.add(check("authentication", "设备认证", authEnabled ? "passed" : "blocked", authEnabled ? "服务端认证已开启；设备专属凭据可在接入与回退中管理" : "服务端认证尚未开启"));
        String ota=params.getValue(Constant.SERVER_OTA,true);
        boolean httpsConfigured=false;
        try {
            DeviceGrantService.validateUrl(ota,false);
            var uri=java.net.URI.create(ota);
            httpsConfigured="https".equals(uri.getScheme()) && !Set.of("127.0.0.1","localhost","::1","[::1]").contains(uri.getHost());
        } catch (RenException | IllegalArgumentException ignored) {
            // Readiness reports configuration only; it never probes arbitrary URLs.
        }
        checks.add(check("ingress", "设备网络入口", httpsConfigured ? "pending" : "blocked",
            httpsConfigured ? "已配置 HTTPS OTA 入口；仍须核实实际设备的网络可达性与证书信任，配置不等于实机验收"
                : "本机隔离入口用于联调，尚未发布实体设备可达的 TLS 网络入口"));
        checks.add(check("firmware", "设备配置与回退", "pending", "尚未读取实际设备的配网设置及原 OTA 地址；不据固件版本推定按键操作"));
        return Map.of("device",device,"checks",checks,"readyToSwitch",false,
            "mode","read_only","configurationChanged",false);
    }

    public Map<String,Object> issue(String deviceId, String clientId, Long owner) {
        if (clientId == null || !clientId.matches("[A-Za-z0-9:_-]{1,128}")) throw new RenException("客户端标识无效");
        var device=connections.deviceInfo(deviceId,owner);
        String mac=device.get("macAddress");
        if (mac == null || !mac.matches("(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}")) throw new RenException("设备 MAC 地址无效");
        String id=connections.selection(deviceId,owner).get("connectionId");
        if (id == null || id.isBlank()) throw new RenException("请先选择订阅语音连接");
        var connection=connections.owned(id,owner);
        if (!"chatgpt_realtime".equals(connection.getKind()) || !Boolean.TRUE.equals(connection.getEnabled())) throw new RenException("订阅语音连接未启用");
        if (!"true".equalsIgnoreCase(params.getValue(Constant.SERVER_AUTH_ENABLED,true))) throw new RenException("必须先启用设备认证");
        if (!"signed_in".equals(accounts.account(id,owner,"status").get("state"))) throw new RenException("订阅尚未授权");
        long timestamp=Instant.now().getEpochSecond();
        String token=NativeDeviceTokens.issue(params.getValue(Constant.SERVER_SECRET,true),owner.toString(),id,clientId,mac,timestamp);
        return Map.of("token",token,"expiresAt",timestamp+900,"scheme","native1","clientId",clientId);
    }
    private static Map<String,String> check(String code,String label,String status,String detail) {
        return Map.of("code",code,"label",label,"status",status,"detail",detail);
    }
}
