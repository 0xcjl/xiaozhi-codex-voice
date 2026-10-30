package xiaozhi.modules.connection;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import org.junit.jupiter.api.Test;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.connection.service.ServiceConnectionService;
import xiaozhi.modules.device.controller.OTAController;
import xiaozhi.modules.device.dto.DeviceReportReqDTO;
import xiaozhi.modules.device.dto.DeviceReportRespDTO;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.sys.service.SysParamsService;

class SubscriptionOtaGuardTest {
    @Test void anonymousMacCannotMintTokenForSubscriptionDevice() {
        var devices=mock(DeviceService.class); var connections=mock(ServiceConnectionService.class);
        var controller=new OTAController(devices,mock(SysParamsService.class),connections);
        var device=new DeviceEntity(); when(devices.getDeviceByMacAddress("02:00:00:00:00:01")).thenReturn(device);
        var connection=new ServiceConnectionEntity(); connection.setKind("chatgpt_realtime");
        when(connections.resolve("02:00:00:00:00:01")).thenReturn(connection);
        var result=controller.checkOTAVersion(new DeviceReportReqDTO(),"02:00:00:00:00:01","client");
        assertEquals(403,result.getStatusCode().value());
        assertFalse(result.getBody().contains("token"));
        verify(devices,never()).checkDeviceActive(anyString(),anyString(),any());
    }
    @Test void ordinaryBoundDeviceKeepsExistingOtaFlow() {
        var devices=mock(DeviceService.class); var connections=mock(ServiceConnectionService.class);
        var controller=new OTAController(devices,mock(SysParamsService.class),connections);
        when(devices.getDeviceByMacAddress("02:00:00:00:00:01")).thenReturn(new DeviceEntity());
        when(devices.checkDeviceActive(anyString(),anyString(),any())).thenReturn(new DeviceReportRespDTO());
        assertEquals(200,controller.checkOTAVersion(new DeviceReportReqDTO(),"02:00:00:00:00:01","client").getStatusCode().value());
        verify(devices).checkDeviceActive(anyString(),anyString(),any());
    }
    @Test void unboundDeviceStillUsesExistingActivationFlow() {
        var devices=mock(DeviceService.class); var connections=mock(ServiceConnectionService.class);
        var controller=new OTAController(devices,mock(SysParamsService.class),connections);
        when(devices.checkDeviceActive(anyString(),anyString(),any())).thenReturn(new DeviceReportRespDTO());
        assertEquals(200,controller.checkOTAVersion(new DeviceReportReqDTO(),"02:00:00:00:00:01","client").getStatusCode().value());
        verifyNoInteractions(connections);
    }
}
