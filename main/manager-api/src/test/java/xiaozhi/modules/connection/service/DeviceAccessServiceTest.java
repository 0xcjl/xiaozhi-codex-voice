package xiaozhi.modules.connection.service;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import xiaozhi.common.constant.Constant;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.sys.service.SysParamsService;

class DeviceAccessServiceTest {
    ServiceConnectionService connections;
    AccountAuthorizationService accounts;
    SysParamsService params;
    DeviceAccessService service;
    ServiceConnectionEntity connection;
    @BeforeEach void setup() {
        connections=mock(ServiceConnectionService.class); accounts=mock(AccountAuthorizationService.class); params=mock(SysParamsService.class);
        service=new DeviceAccessService(connections,accounts,params);
        when(connections.deviceInfo("device",123L)).thenReturn(Map.of("id","device","macAddress","02:00:00:00:00:01"));
        when(connections.selection("device",123L)).thenReturn(Map.of("connectionId","connection"));
        connection=new ServiceConnectionEntity();connection.setKind("chatgpt_realtime");connection.setEnabled(true);
        when(connections.owned("connection",123L)).thenReturn(connection);
        when(accounts.account("connection",123L,"status")).thenReturn(Map.of("state","signed_in"));
        when(params.getValue(Constant.SERVER_AUTH_ENABLED,true)).thenReturn("true");
        when(params.getValue(Constant.SERVER_SECRET,true)).thenReturn("synthetic-test-server-secret");
    }
    @Test void javaNativeTokenMatchesPythonVector() {
        assertEquals("native1.Po_eflAyR-EMzHoHCHerr9P7rcMxFFRgPxJz5a0JH6A.1000",
            NativeDeviceTokens.issue("synthetic-test-server-secret","123","connection","client","02:00:00:00:00:01",1000));
    }
    @Test void issuanceRequiresExistingDeviceOwnership() {
        when(connections.deviceInfo("foreign",123L)).thenThrow(new RenException("denied"));
        assertThrows(RenException.class,()->service.issue("foreign","client",123L));verifyNoInteractions(accounts,params);
    }
    @Test void enabledConnectionAndSignedInAccountAreRequired() {
        connection.setEnabled(false); assertThrows(RenException.class,()->service.issue("device","client",123L));
        connection.setEnabled(true);when(accounts.account("connection",123L,"status")).thenReturn(Map.of("state","signed_out"));
        assertThrows(RenException.class,()->service.issue("device","client",123L));
    }
    @Test void authenticationCannotBeDisabledForIssuance() {
        when(params.getValue(Constant.SERVER_AUTH_ENABLED,true)).thenReturn("false");
        assertThrows(RenException.class,()->service.issue("device","client",123L)); verifyNoInteractions(accounts);
    }
    @Test void clientIdentifierCannotInjectSignatureDelimiters() {
        assertThrows(RenException.class,()->service.issue("device","client|other",123L));
        verifyNoInteractions(connections,accounts,params);
    }
    @Test void tokenContainsNoSubscriptionCredential() {
        var issued=service.issue("device","client",123L);
        assertEquals("native1",issued.get("scheme"));assertTrue(issued.get("token").toString().startsWith("native1."));
        assertEquals(4,issued.size());
    }
    @Test void readinessCannotClaimADeviceWasSwitchedOrNetworkPublished() {
        var result=service.readiness("device",123L);
        assertEquals(false,result.get("readyToSwitch"));assertEquals(false,result.get("configurationChanged"));
        assertEquals("read_only",result.get("mode"));assertTrue(result.get("checks").toString().contains("not_published") || result.get("checks").toString().contains("尚未发布"));
    }
    @Test void configuredHttpsIngressStillRequiresDeviceVerification() {
        when(params.getValue(Constant.SERVER_OTA,true)).thenReturn("https://robot.0xjialin.com/xiaozhi/ota/");
        var result=service.readiness("device",123L);
        assertEquals(false,result.get("readyToSwitch"));
        assertTrue(result.get("checks").toString().contains("已配置 HTTPS"));
        assertFalse(result.get("checks").toString().contains("尚未发布"));
    }
    @Test void loopbackIngressMustNotLookDeviceReachable() {
        when(params.getValue(Constant.SERVER_OTA,true)).thenReturn("http://127.0.0.1:18823/xiaozhi/ota/");
        assertTrue(service.readiness("device",123L).get("checks").toString().contains("尚未发布"));
    }
}
