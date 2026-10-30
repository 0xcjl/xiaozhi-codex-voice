package xiaozhi.modules.connection.service;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import java.time.Instant;
import java.util.*;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.sys.service.SysParamsService;

class DeviceGrantServiceTest {
    JdbcTemplate db;ServiceConnectionService connections;AccountAuthorizationService accounts;SysParamsService params;DeviceGrantService service;
    ServiceConnectionEntity connection;Map<String,Object> row;
    String token="native2."+"a".repeat(43);
    @BeforeEach void setup(){
        db=mock(JdbcTemplate.class);connections=mock(ServiceConnectionService.class);accounts=mock(AccountAuthorizationService.class);params=mock(SysParamsService.class);
        service=new DeviceGrantService(db,connections,accounts,params);
        when(connections.deviceInfo("device",123L)).thenReturn(Map.of("macAddress","02:00:00:00:00:01"));
        when(connections.selection("device",123L)).thenReturn(Map.of("connectionId","connection"));
        connection=new ServiceConnectionEntity();connection.setId("connection");connection.setUserId(123L);connection.setKind("chatgpt_realtime");connection.setRuntimeId("local");connection.setModel("gpt-6.1-terra");connection.setEnabled(true);connection.setRevision(1);
        when(connections.owned("connection",123L)).thenReturn(connection);
        when(accounts.account("connection",123L,"status")).thenReturn(Map.of("state","signed_in"));
        when(params.getValue(Constant.SERVER_AUTH_ENABLED,true)).thenReturn("true");
        when(params.getValue(Constant.SERVER_OTA,true)).thenReturn("http://127.0.0.1:18823/xiaozhi/ota/");
        when(params.getValue(Constant.SERVER_WEBSOCKET,true)).thenReturn("ws://127.0.0.1:18824/xiaozhi/v1/");
        row=new HashMap<>(Map.of("device_id","device","owner_id",123L,"connection_id","connection","connection_revision",1,"client_id","client","expires_at",Instant.now().getEpochSecond()+3600,"revoked",0));
        when(db.queryForList(contains("FOR UPDATE"),any(Object[].class))).thenReturn(List.of(row));
    }
    @Test void insecureOrCredentialBearingUrlsAreRejected(){
        for(String url:List.of("http://192.168.1.2/ota/","https://user:pw@host/","https://host/?key=secret","https://host/ota/native/secret","not a url"))
            assertThrows(RenException.class,()->DeviceGrantService.validateUrl(url,false));
        DeviceGrantService.validateUrl("https://api.tenclass.net/xiaozhi/ota/",false);
    }
    @Test void grantIsHashedAndOriginalAddressIsNotInvented(){
        var result=service.provision("device",123L,"","");
        assertEquals(false,result.get("rollbackAvailable"));assertEquals(false,result.get("deviceChanged"));
        assertTrue(result.get("otaUrl").toString().contains("/native/native2."));
        var call=mockingDetails(db).getInvocations().stream().filter(i->i.getMethod().getName().equals("update")).findFirst().orElseThrow();
        assertFalse(Arrays.toString(call.getArguments()).contains(result.get("otaUrl").toString().split("/native/")[1]));
    }
    @Test void foreignDeviceCannotIssueOrRevoke(){
        when(connections.deviceInfo("foreign",123L)).thenThrow(new RenException("denied"));
        assertThrows(RenException.class,()->service.provision("foreign",123L,"",""));
        assertThrows(RenException.class,()->service.revoke("foreign",123L));verifyNoInteractions(db);
    }
    @Test void expiredAndRevokedAndWrongClientAreRejected(){
        row.put("revoked",1);assertThrows(RenException.class,()->service.verify(token,"02:00:00:00:00:01","client"));
        row.put("revoked",0);row.put("expires_at",1L);assertThrows(RenException.class,()->service.verify(token,"02:00:00:00:00:01","client"));
        row.put("expires_at",Instant.now().getEpochSecond()+100);assertThrows(RenException.class,()->service.verify(token,"02:00:00:00:00:01","other"));
    }
    @Test void wrongMacAndChangedRevisionAreRejected(){
        assertThrows(RenException.class,()->service.verify(token,"02:00:00:00:00:02","client"));
        connection.setRevision(2);assertThrows(RenException.class,()->service.verify(token,"02:00:00:00:00:01","client"));
    }
    @Test void firstValidUsePinsClientAndRenewsLease(){
        row.put("client_id","");var result=service.verify(token,"02:00:00:00:00:01","client");
        assertEquals(true,result.get("valid"));verify(db).update(contains("SET client_id=?"),eq("client"),anyLong(),eq("device"));
    }
    @Test void otaNeverReturnsFirmwareMqttOrSubscriptionCredential(){
        var result=service.ota(token,"02:00:00:00:00:01","client");
        assertEquals(Set.of("websocket"),result.keySet());assertEquals(token,((Map<?,?>)result.get("websocket")).get("token"));
    }
    @Test void revokedCredentialCannotBeRenewedSilently(){
        assertThrows(RenException.class,()->service.renew("device",123L));
    }
    @Test void fixedDomainGrantUsesPublicOtaWithoutChangingRollbackState(){
        when(params.getValue(Constant.SERVER_OTA,true)).thenReturn("https://robot.0xjialin.com/xiaozhi/ota/");
        var result=service.provision("device",123L,"client","");
        assertTrue(result.get("otaUrl").toString().startsWith("https://robot.0xjialin.com/xiaozhi/ota/native/native2."));
        assertEquals(false,result.get("rollbackAvailable"));assertEquals(false,result.get("deviceChanged"));
    }
}
