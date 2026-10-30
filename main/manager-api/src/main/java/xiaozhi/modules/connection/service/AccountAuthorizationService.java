package xiaozhi.modules.connection.service;

import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import com.fasterxml.jackson.databind.ObjectMapper;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.sys.service.SysParamsService;

@Service
public class AccountAuthorizationService {
    private final ServiceConnectionService connections;
    private final SysParamsService params;
    private final ObjectMapper json;
    private final String runtimeUrl;
    private final HttpClient client;

    public AccountAuthorizationService(ServiceConnectionService connections, SysParamsService params,
            ObjectMapper json, @Value("${service-connections.runtime-url:}") String runtimeUrl) {
        this.connections = connections; this.params = params; this.json = json; this.runtimeUrl = runtimeUrl;
        this.client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).followRedirects(HttpClient.Redirect.NEVER).build();
    }

    public Map<String, Object> account(String id, Long owner, String action) {
        var connection = connections.owned(id, owner);
        if (!"chatgpt_realtime".equals(connection.getKind()) || !"local".equals(connection.getRuntimeId()))
            throw new RenException("请选择本机 ChatGPT 实时语音连接");
        if (!Set.of("status", "start", "cancel", "refresh", "reauthorize").contains(action)) throw new RenException("不支持的授权操作");
        if (runtimeUrl == null || runtimeUrl.isBlank()) throw new RenException("尚未配置订阅授权运行节点");
        try {
            URI base = URI.create(runtimeUrl);
            boolean loopback = Set.of("127.0.0.1", "localhost", "[::1]").contains(base.getHost());
            if (base.getUserInfo() != null || base.getQuery() != null || base.getFragment() != null
                    || !("https".equals(base.getScheme()) || ("http".equals(base.getScheme()) && loopback)))
                throw new RenException("运行节点必须使用 HTTPS 或回环地址");
            String secret = params.getValue(Constant.SERVER_SECRET, true);
            if (secret == null || secret.length() < 24) throw new RenException("请配置独立服务端密钥");
            String body = json.writeValueAsString(Map.of("ownerId", owner.toString(), "connectionId", id));
            var request = HttpRequest.newBuilder(base.resolve("/internal/codex/accounts/" + action))
                .timeout(Duration.ofSeconds(25)).header("Authorization", "Bearer " + secret)
                .header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(body)).build();
            var response = client.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 403) throw new RenException("运行节点尚未许可当前控制台用户");
            if (response.statusCode() != 200) throw new RenException("订阅授权服务暂不可用，请稍后重试");
            return publicStatus(json.readValue(response.body(), Map.class));
        } catch (RenException e) { throw e; }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new RenException("授权请求已中断"); }
        catch (Exception e) { throw new RenException("订阅授权连接失败，请检查运行节点"); }
    }

    // Explicit allowlist: upstream tokens/errors must never pass through to the browser.
    static Map<String, Object> publicStatus(Map<?, ?> input) {
        Object state = input.get("state");
        if (!(state instanceof String) || !Set.of("signed_out", "signed_in", "pending", "expired", "cancelled", "failed").contains(state))
            throw new RenException("授权服务返回了无效状态");
        Map<String, Object> result = new HashMap<>(); result.put("state", state);
        if ("signed_in".equals(state)) {
            for (String field : List.of("email", "planType")) {
                Object value = input.get(field);
                if (value instanceof String text && text.length() <= 254) result.put(field, text);
            }
        }
        if ("pending".equals(state) && input.containsKey("userCode")) {
            if (!"https://auth.openai.com/codex/device".equals(input.get("verificationUrl"))
                    || !(input.get("userCode") instanceof String code) || !code.matches("[A-Z0-9-]{4,32}"))
                throw new RenException("授权服务返回了无效登录地址");
            result.put("verificationUrl", input.get("verificationUrl")); result.put("userCode", input.get("userCode"));
            result.put("expiresInSeconds", 600);
        }
        return result;
    }
}
