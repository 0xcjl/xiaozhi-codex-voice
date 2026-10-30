package xiaozhi.modules.connection.entity;

import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

@Data
@TableName("ai_device_connection")
public class DeviceConnectionEntity {
    @TableId
    private String deviceId;
    private String connectionId;
}
