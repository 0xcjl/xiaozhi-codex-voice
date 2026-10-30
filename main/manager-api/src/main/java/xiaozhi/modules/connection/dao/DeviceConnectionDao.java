package xiaozhi.modules.connection.dao;

import org.apache.ibatis.annotations.Mapper;
import xiaozhi.common.dao.BaseDao;
import xiaozhi.modules.connection.entity.DeviceConnectionEntity;

@Mapper
public interface DeviceConnectionDao extends BaseDao<DeviceConnectionEntity> {
}
