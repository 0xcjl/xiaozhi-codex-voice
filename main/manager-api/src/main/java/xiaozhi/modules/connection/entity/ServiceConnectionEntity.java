package xiaozhi.modules.connection.entity;

import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

@Data
@TableName("ai_service_connection")
public class ServiceConnectionEntity {
    private String id;
    private Long userId;
    private String name;
    private String kind;
    private String modelId;
    private String runtimeId;
    private String model;
    private Boolean enabled;
    private Integer revision;
}
