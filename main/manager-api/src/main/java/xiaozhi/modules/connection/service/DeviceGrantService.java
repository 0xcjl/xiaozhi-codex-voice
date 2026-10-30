package xiaozhi.modules.connection.service;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.time.Instant;
import java.util.*;
import lombok.RequiredArgsConstructor;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.sys.service.SysParamsService;

/** Stock firmware keeps this credential in NVS; lease renewal stays server-side. */
@Service
@RequiredArgsConstructor
public class DeviceGrantService {
    static final long LEASE_SECONDS = 30L * 24 * 3600;
    private final JdbcTemplate db;
    private final ServiceConnectionService connections;
    private final AccountAuthorizationService accounts;
    private final SysParamsService params;

    @Transactional
    public Map<String,Object> provision(String device, Long owner, String client, String originalOta) {
        var info=connections.deviceInfo(device,owner);
        if (!info.get("macAddress").matches("(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}")) throw denied();
        if (client == null || !client.matches("[A-Za-z0-9:_-]{0,128}")) throw denied();
        String original=originalOta == null ? "" : originalOta.trim();
        if (!original.isEmpty()) validateUrl(original,false);
        var connection=selected(device,owner);
        if (!"signed_in".equals(accounts.account(connection.getId(),owner,"status").get("state"))) throw new RenException("请先完成独立订阅授权");
        String base=params.getValue(Constant.SERVER_OTA,true);
        validateUrl(base,false);
        if(base.replaceAll("/+$","").length()+8+51>255) throw new RenException("接入地址超出标准配网组件长度限制");
        validateUrl(params.getValue(Constant.SERVER_WEBSOCKET,true),true);
        byte[] random=new byte[32];new SecureRandom().nextBytes(random);
        String token="native2."+Base64.getUrlEncoder().withoutPadding().encodeToString(random);
        long expires=Instant.now().getEpochSecond()+LEASE_SECONDS;
        db.update("INSERT INTO ai_native_device_grant (device_id,owner_id,connection_id,connection_revision,token_hash,client_id,expires_at,revoked,original_ota_url) VALUES (?,?,?,?,?,?,?,FALSE,?) ON DUPLICATE KEY UPDATE owner_id=VALUES(owner_id),connection_id=VALUES(connection_id),connection_revision=VALUES(connection_revision),token_hash=VALUES(token_hash),client_id=VALUES(client_id),expires_at=VALUES(expires_at),revoked=FALSE,original_ota_url=VALUES(original_ota_url)",
            device,owner,connection.getId(),connection.getRevision(),hash(token),client,expires,original);
        return Map.of("otaUrl",base.replaceAll("/+$","")+"/native/"+token,"expiresAt",expires,
            "credentialDisplayedOnce",true,"deviceChanged",false,"rollbackAvailable",!original.isEmpty());
    }

    public Map<String,Object> status(String device,Long owner) {
        connections.deviceInfo(device,owner);
        var rows=db.queryForList("SELECT client_id,expires_at,revoked,original_ota_url FROM ai_native_device_grant WHERE device_id=? AND owner_id=?",device,owner);
        if (rows.isEmpty()) return Map.of("state","not_configured","deviceChanged",false);
        var row=rows.getFirst();
        boolean revoked=isRevoked(row.get("revoked"));
        long expires=((Number)row.get("expires_at")).longValue();
        String original=(String)row.get("original_ota_url");
        return Map.of("state",revoked?"revoked":expires<Instant.now().getEpochSecond()?"expired":"active",
            "clientId",row.get("client_id"),"expiresAt",expires,"originalOtaUrl",original,
            "rollbackAvailable",!original.isEmpty(),"deviceChanged",false);
    }

    public void revoke(String device,Long owner) {
        connections.deviceInfo(device,owner);
        db.update("UPDATE ai_native_device_grant SET revoked=TRUE WHERE device_id=? AND owner_id=?",device,owner);
    }

    @Transactional
    public void renew(String device,Long owner) {
        var connection=selected(device,owner);
        if (!"signed_in".equals(accounts.account(connection.getId(),owner,"status").get("state"))) throw denied();
        int count=db.update("UPDATE ai_native_device_grant SET expires_at=? WHERE device_id=? AND owner_id=? AND revoked=FALSE AND connection_id=? AND connection_revision=?",
            Instant.now().getEpochSecond()+LEASE_SECONDS,device,owner,connection.getId(),connection.getRevision());
        if(count!=1) throw new RenException("凭据已撤销或连接已改变，请重新生成接入地址");
    }

    @Transactional
    public Map<String,Object> verify(String token,String mac,String client) {
        if(token==null || !token.matches("native2\\.[A-Za-z0-9_-]{43}") || mac==null || !mac.matches("(?i)([0-9a-f]{2}:){5}[0-9a-f]{2}") || client==null || !client.matches("[A-Za-z0-9:_-]{1,128}")) throw denied();
        var rows=db.queryForList("SELECT * FROM ai_native_device_grant WHERE token_hash=? FOR UPDATE",hash(token));
        if(rows.size()!=1) throw denied();
        var row=rows.getFirst();long now=Instant.now().getEpochSecond();
        if(isRevoked(row.get("revoked")) || ((Number)row.get("expires_at")).longValue()<now) throw denied();
        String device=(String)row.get("device_id");Long owner=((Number)row.get("owner_id")).longValue();
        var info=connections.deviceInfo(device,owner);
        if(!mac.equalsIgnoreCase(info.get("macAddress"))) throw denied();
        var connection=selected(device,owner);
        if(!connection.getId().equals(row.get("connection_id")) || connection.getRevision()!=((Number)row.get("connection_revision")).intValue()) throw denied();
        String pinned=(String)row.get("client_id");
        if(!pinned.isEmpty() && !client.equals(pinned)) throw denied();
        // Row lock serializes first-use pinning, renewal and revocation.
        if(pinned.isEmpty() || ((Number)row.get("expires_at")).longValue()<now+LEASE_SECONDS-3600)
            db.update("UPDATE ai_native_device_grant SET client_id=?,expires_at=? WHERE device_id=?",client,now+LEASE_SECONDS,device);
        return Map.of("connectionId",connection.getId(),"userId",owner,"valid",true);
    }

    @Transactional
    public Map<String,Object> ota(String token,String mac,String client) {
        var verified=verify(token,mac,client);
        if(!"signed_in".equals(accounts.account(verified.get("connectionId").toString(),((Number)verified.get("userId")).longValue(),"status").get("state"))) throw denied();
        String url=params.getValue(Constant.SERVER_WEBSOCKET,true);validateUrl(url,true);
        // Omit firmware, MQTT and activation: never flash or silently fall back.
        return Map.of("websocket",Map.of("url",url,"token",token,"version",2));
    }

    private xiaozhi.modules.connection.entity.ServiceConnectionEntity selected(String device,Long owner) {
        String id=connections.selection(device,owner).get("connectionId");
        if(id==null || id.isBlank()) throw denied();
        var connection=connections.owned(id,owner);
        if(!"chatgpt_realtime".equals(connection.getKind()) || !Boolean.TRUE.equals(connection.getEnabled()) || !"local".equals(connection.getRuntimeId()) || !"gpt-6.1-terra".equals(connection.getModel()) || !"true".equalsIgnoreCase(params.getValue(Constant.SERVER_AUTH_ENABLED,true))) throw denied();
        return connection;
    }
    static void validateUrl(String value,boolean websocket) {
        try {
            if(value==null || value.length()>1024) throw denied();
            URI u=URI.create(value);String host=u.getHost();
            boolean secure=(websocket?"wss":"https").equals(u.getScheme());
            boolean local=(websocket?"ws":"http").equals(u.getScheme()) && Set.of("127.0.0.1","localhost","::1","[::1]").contains(host==null?"":host);
            if((!secure && !local) || host==null || u.getUserInfo()!=null || u.getQuery()!=null || u.getFragment()!=null || value.contains("/native/")) throw denied();
        } catch(IllegalArgumentException e) {throw denied();}
    }
    static String hash(String token) {
        try {return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.UTF_8)));}
        catch(Exception e){throw new IllegalStateException("SHA-256 unavailable");}
    }
    private static boolean isRevoked(Object value) {return Boolean.TRUE.equals(value) || value instanceof Number n && n.intValue()!=0;}
    private static RenException denied(){return new RenException("设备访问未获授权或配置无效");}
}
