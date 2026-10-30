package xiaozhi.modules.connection;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import java.util.Map;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.apache.shiro.authz.annotation.RequiresPermissions;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.connection.controller.ServiceConnectionController;
import xiaozhi.modules.connection.dao.*;
import xiaozhi.modules.connection.dto.ConnectionBody;
import xiaozhi.modules.connection.entity.*;
import xiaozhi.modules.connection.service.ServiceConnectionService;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.model.entity.ModelConfigEntity;
import xiaozhi.modules.model.service.ModelConfigService;

class ServiceConnectionServiceTest {
    ServiceConnectionDao connections;
    DeviceConnectionDao bindings;
    DeviceService devices;
    ModelConfigService models;
    ServiceConnectionService service;
    @BeforeEach void setup() {
        connections = mock(ServiceConnectionDao.class); bindings = mock(DeviceConnectionDao.class);
        devices = mock(DeviceService.class); models = mock(ModelConfigService.class);
        service = new ServiceConnectionService(connections, bindings, devices, models);
    }
    ConnectionBody nativeBody(int revision) {
        return new ConnectionBody("本机语音", "chatgpt_realtime", null, "local", "gpt-6.1-terra", true, revision);
    }
    ServiceConnectionEntity existing() {
        var item = new ServiceConnectionEntity(); item.setId("c1"); item.setUserId(1L);
        item.setRevision(1); item.setKind("chatgpt_realtime"); item.setEnabled(true);
        when(connections.selectById("c1")).thenReturn(item); return item;
    }
    DeviceEntity device(Long owner) {
        var d = new DeviceEntity(); d.setId("d1"); d.setUserId(owner); d.setMacAddress("aa:bb:cc:dd:ee:ff");
        when(devices.selectById("d1")).thenReturn(d); when(devices.getDeviceByMacAddress(d.getMacAddress())).thenReturn(d); return d;
    }
    @Test void requiresAdminForAllManagementEndpoints() {
        assertArrayEquals(new String[]{"sys:role:superAdmin"}, ServiceConnectionController.class.getAnnotation(RequiresPermissions.class).value());
    }
    @Test void savesNativeMetadataWithoutCredentials() {
        var result = service.save(null, nativeBody(0), 1L);
        assertEquals("gpt-6.1-terra", result.getModel()); assertNull(result.getModelId());
        assertEquals(1, result.getRevision()); assertEquals(1L, result.getUserId()); verify(connections).insert(result);
    }
    @Test void rejectsForeignConnection() {
        existing(); assertThrows(RenException.class, () -> service.save("c1", nativeBody(1), 2L));
        verify(connections, never()).update(any(), any());
    }
    @Test void rejectsForeignDeviceEvenForAdmin() {
        device(2L); assertThrows(RenException.class, () -> service.select("d1", "c1", 1L)); verifyNoInteractions(bindings);
    }
    @Test void rejectsUnregisteredRuntimeAndArbitraryModel() {
        assertThrows(RenException.class, () -> service.save(null, new ConnectionBody("x", "chatgpt_realtime", null, "http://evil", "gpt-6.1-terra", true, 0), 1L));
        assertThrows(RenException.class, () -> service.save(null, new ConnectionBody("x", "chatgpt_realtime", null, "local", "other", true, 0), 1L));
    }
    @Test void rejectsDisabledOrWrongTypeApiModel() {
        var model = new ModelConfigEntity(); model.setModelType("TTS"); model.setIsEnabled(1);
        when(models.selectById("m1")).thenReturn(model);
        var body = new ConnectionBody("API", "api", "m1", null, null, true, 0);
        assertThrows(RenException.class, () -> service.save(null, body, 1L));
        model.setModelType("LLM"); model.setIsEnabled(0);
        assertThrows(RenException.class, () -> service.save(null, body, 1L));
    }
    @Test void apiReferenceDoesNotReturnProviderSecret() {
        var model = new ModelConfigEntity(); model.setModelType("LLM"); model.setIsEnabled(1);
        when(models.selectById("m1")).thenReturn(model);
        var saved = service.save(null, new ConnectionBody("API", "api", "m1", null, null, true, 0), 1L);
        assertEquals("m1", saved.getModelId()); assertNull(saved.getRuntimeId());
    }
    @Test void staleRevisionAndLostCompareAndSwapAreRejected() {
        existing(); assertThrows(RenException.class, () -> service.save("c1", nativeBody(0), 1L));
        when(connections.update(isNull(), any())).thenReturn(0);
        assertThrows(RenException.class, () -> service.save("c1", nativeBody(1), 1L));
    }
    @Test void clearingSelectionRestoresOriginalConfiguration() {
        device(1L); service.select("d1", "", 1L); verify(bindings).deleteById("d1"); verifyNoInteractions(connections);
    }
    @Test void disabledSelectionFailsClosed() {
        device(1L); var item = existing(); item.setEnabled(false);
        assertThrows(RenException.class, () -> service.select("d1", "c1", 1L));
    }
    @Test void runtimeChecksCurrentDeviceOwnerAfterRebinding() {
        device(2L); existing(); var binding = new DeviceConnectionEntity(); binding.setConnectionId("c1");
        when(bindings.selectById("d1")).thenReturn(binding);
        assertThrows(RenException.class, () -> service.resolve("aa:bb:cc:dd:ee:ff"));
    }
    @Test void noBindingResolvesToDefault() {
        device(1L); assertNull(service.resolve("aa:bb:cc:dd:ee:ff"));
        assertEquals(Map.of("connectionId", ""), service.selection("d1", 1L));
    }
}
