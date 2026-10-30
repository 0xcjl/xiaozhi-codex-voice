package xiaozhi.modules.connection.service;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import xiaozhi.common.exception.RenException;

/** Separate audience from the generic token minted by unauthenticated OTA. */
public final class NativeDeviceTokens {
    private NativeDeviceTokens() {}
    public static String issue(String secret, String owner, String connection, String client, String mac, long timestamp) {
        if (secret == null || secret.length() < 24) throw new RenException("服务端密钥未配置");
        try {
            byte[] key = hmac(secret.getBytes(StandardCharsets.UTF_8), "xiaozhi-native-v1|" + owner + "|" + connection);
            byte[] signature = hmac(key, client + "|" + mac.toLowerCase(java.util.Locale.ROOT) + "|" + timestamp);
            return "native1." + Base64.getUrlEncoder().withoutPadding().encodeToString(signature) + "." + timestamp;
        } catch (Exception e) { throw new RenException("设备凭据生成失败"); }
    }
    private static byte[] hmac(byte[] key, String text) throws Exception {
        Mac signer = Mac.getInstance("HmacSHA256");
        signer.init(new SecretKeySpec(key, "HmacSHA256"));
        return signer.doFinal(text.getBytes(StandardCharsets.UTF_8));
    }
}
