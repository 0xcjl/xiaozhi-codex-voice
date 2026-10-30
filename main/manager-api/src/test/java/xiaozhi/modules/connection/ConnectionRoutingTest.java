package xiaozhi.modules.connection;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
import java.util.Map;
import org.junit.jupiter.api.Test;
import xiaozhi.common.redis.RedisUtils;
import xiaozhi.modules.config.service.impl.ConfigServiceImpl;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;
import xiaozhi.modules.connection.service.ServiceConnectionService;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.model.service.ModelConfigService;
import xiaozhi.modules.model.entity.ModelConfigEntity;
import xiaozhi.modules.agent.service.*;
import xiaozhi.modules.agent.vo.AgentInfoVO;
import xiaozhi.modules.agent.dao.AgentVoicePrintDao;
import xiaozhi.modules.sys.service.SysParamsService;
import xiaozhi.modules.timbre.service.TimbreService;
import xiaozhi.modules.voiceclone.service.VoiceCloneService;
import xiaozhi.modules.correctword.service.CorrectWordFileService;
import cn.hutool.json.JSONObject;

class ConnectionRoutingTest {
    @Test void apiChoiceChangesRuntimeModelWithoutChangingAgent() { check("api", "chosen-api"); }
    @Test void noChoiceRetainsOriginalAgentModel() { check(null, "original"); }
    private void check(String kind, String expected) {
        var connections = mock(ServiceConnectionService.class);
        var devices = mock(DeviceService.class); var models = mock(ModelConfigService.class);
        var agents = mock(AgentService.class);
        var service = new ConfigServiceImpl(mock(SysParamsService.class), connections, devices, models,
            agents, mock(AgentTemplateService.class), mock(RedisUtils.class), mock(TimbreService.class),
            mock(AgentPluginMappingService.class), mock(AgentMcpAccessPointService.class),
            mock(AgentContextProviderService.class), mock(VoiceCloneService.class), mock(AgentVoicePrintDao.class), mock(CorrectWordFileService.class));
        var device = new DeviceEntity(); device.setId("device"); device.setAgentId("agent");
        when(devices.getDeviceByMacAddress("mac")).thenReturn(device);
        var agent = new AgentInfoVO(); agent.setId("agent"); agent.setLlmModelId("original"); agent.setIntentModelId("Intent_nointent");
        when(agents.getAgentById("agent")).thenReturn(agent);
        if (kind != null) {
            var selected = new ServiceConnectionEntity(); selected.setKind(kind); selected.setModelId("chosen-api");
            when(connections.resolve("mac")).thenReturn(selected);
        }
        var model = new ModelConfigEntity(); model.setId(expected); model.setConfigJson(new JSONObject());
        when(models.getModelByIdFromCache(expected)).thenReturn(model);
        var result = service.getAgentModels("mac", Map.of());
        assertEquals(expected, ((Map<?, ?>)result.get("selected_module")).get("LLM"));
        assertEquals("original", agent.getLlmModelId());
        verify(models).getModelByIdFromCache(expected);
    }
}
