package xiaozhi.modules.connection.service;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import com.baomidou.mybatisplus.core.conditions.query.QueryWrapper;
import com.baomidou.mybatisplus.core.conditions.update.UpdateWrapper;
import lombok.AllArgsConstructor;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import xiaozhi.common.exception.RenException;
import xiaozhi.modules.connection.dao.*;
import xiaozhi.modules.connection.dto.ConnectionBody;
import xiaozhi.modules.connection.entity.*;
import xiaozhi.modules.device.entity.DeviceEntity;
import xiaozhi.modules.device.service.DeviceService;
import xiaozhi.modules.model.entity.ModelConfigEntity;
import xiaozhi.modules.model.service.ModelConfigService;

@Service
@AllArgsConstructor
public class ServiceConnectionService {
    private final ServiceConnectionDao connections;
    private final DeviceConnectionDao bindings;
    private final DeviceService devices;
    private final ModelConfigService models;

    public List<Map<String, String>> devices(Long owner) {
        return devices.searchDevicesByMacAddress("", owner).stream()
            .map(d -> Map.of("id", d.getId(), "macAddress", d.getMacAddress(),
                "name", d.getAlias() == null ? d.getMacAddress() : d.getAlias())).toList();
    }

    public List<ServiceConnectionEntity> list(Long owner) {
        return connections.selectList(new QueryWrapper<ServiceConnectionEntity>().eq("user_id", owner).orderByAsc("name", "id"));
    }

    public ServiceConnectionEntity owned(String id, Long owner) {
        ServiceConnectionEntity item = connections.selectById(id);
        if (item == null || !Objects.equals(item.getUserId(), owner))
            throw new RenException("服务连接不存在或无权限");
        return item;
    }

    @Transactional
    public ServiceConnectionEntity save(String id, ConnectionBody body, Long owner) {
        ServiceConnectionEntity item = id == null ? new ServiceConnectionEntity() : owned(id, owner);
        if (id == null && body.revision() != 0) throw new RenException("新连接的修订号必须为零");
        if (id != null && !Objects.equals(item.getRevision(), body.revision())) throw new RenException("配置已被更新，请刷新后重试");
        if ("api".equals(body.kind())) {
            validateModel(body.modelId());
            if (notEmpty(body.runtimeId()) || notEmpty(body.model())) throw new RenException("API 连接使用已有模型配置");
        } else if ("chatgpt_realtime".equals(body.kind())) {
            if (!"local".equals(body.runtimeId()) || notEmpty(body.modelId())) throw new RenException("当前仅支持服务端注册的 local 节点");
            if (!"gpt-6.1-terra".equals(body.model())) throw new RenException("当前语音服务执行模型为 gpt-6.1-terra");
        } else throw new RenException("不支持的服务类型");
        item.setName(body.name().trim());
        if (item.getName().isEmpty()) throw new RenException("名称不能为空");
        item.setUserId(owner);
        item.setKind(body.kind());
        item.setModelId("api".equals(body.kind()) ? body.modelId() : null);
        item.setRuntimeId("api".equals(body.kind()) ? null : body.runtimeId());
        item.setModel("api".equals(body.kind()) ? null : body.model());
        item.setEnabled(body.enabled());
        item.setRevision(body.revision() + 1);
        if (id == null) {
            item.setId(UUID.randomUUID().toString());
            connections.insert(item);
        } else {
            // Explicit SET includes nulls when switching connection types.
            int updated = connections.update(null, new UpdateWrapper<ServiceConnectionEntity>()
                .eq("id", id).eq("user_id", owner).eq("revision", body.revision())
                .set("name", item.getName()).set("kind", item.getKind()).set("model_id", item.getModelId())
                .set("runtime_id", item.getRuntimeId()).set("model", item.getModel())
                .set("enabled", item.getEnabled()).set("revision", item.getRevision()));
            if (updated != 1) throw new RenException("配置已被更新，请刷新后重试");
        }
        return item;
    }

    private static boolean notEmpty(String value) { return value != null && !value.isBlank(); }

    private void validateModel(String id) {
        ModelConfigEntity model = id == null ? null : models.selectById(id);
        if (model == null || !"LLM".equalsIgnoreCase(model.getModelType()) || !Integer.valueOf(1).equals(model.getIsEnabled()))
            throw new RenException("请选择已启用的 LLM 模型");
    }

    private DeviceEntity ownedDevice(String id, Long owner) {
        DeviceEntity device = devices.selectById(id);
        if (device == null || !Objects.equals(device.getUserId(), owner)) throw new RenException("设备不存在或无权限");
        return device;
    }

    public Map<String, String> deviceInfo(String deviceId, Long owner) {
        DeviceEntity device = ownedDevice(deviceId, owner);
        return Map.of("id", device.getId(), "name", device.getAlias() == null ? "" : device.getAlias(),
            "macAddress", device.getMacAddress() == null ? "" : device.getMacAddress(),
            "firmwareVersion", device.getAppVersion() == null ? "" : device.getAppVersion(),
            "board", device.getBoard() == null ? "" : device.getBoard());
    }

    public Map<String, String> selection(String deviceId, Long owner) {
        ownedDevice(deviceId, owner);
        DeviceConnectionEntity binding = bindings.selectById(deviceId);
        return Map.of("connectionId", binding == null ? "" : binding.getConnectionId());
    }

    @Transactional
    public void select(String deviceId, String connectionId, Long owner) {
        ownedDevice(deviceId, owner);
        if (connectionId == null || connectionId.isBlank()) {
            bindings.deleteById(deviceId);
            return;
        }
        ServiceConnectionEntity connection = owned(connectionId, owner);
        if (!Boolean.TRUE.equals(connection.getEnabled())) throw new RenException("连接已停用");
        if ("api".equals(connection.getKind())) validateModel(connection.getModelId());
        DeviceConnectionEntity binding = new DeviceConnectionEntity();
        binding.setDeviceId(deviceId);
        binding.setConnectionId(connectionId);
        if (bindings.selectById(deviceId) == null) bindings.insert(binding);
        else bindings.updateById(binding);
    }

    /** Trusted runtime only. Never returns API keys or subscription tokens. */
    public ServiceConnectionEntity resolve(String mac) {
        DeviceEntity device = devices.getDeviceByMacAddress(mac);
        if (device == null) throw new RenException("设备不存在");
        DeviceConnectionEntity binding = bindings.selectById(device.getId());
        if (binding == null) return null;
        ServiceConnectionEntity connection = owned(binding.getConnectionId(), device.getUserId());
        if (!Boolean.TRUE.equals(connection.getEnabled())) throw new RenException("所选服务连接已停用");
        if ("api".equals(connection.getKind())) validateModel(connection.getModelId());
        return connection;
    }
}
