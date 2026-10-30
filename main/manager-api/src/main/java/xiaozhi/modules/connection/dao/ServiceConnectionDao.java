package xiaozhi.modules.connection.dao;

import org.apache.ibatis.annotations.Mapper;
import xiaozhi.common.dao.BaseDao;
import xiaozhi.modules.connection.entity.ServiceConnectionEntity;

@Mapper
public interface ServiceConnectionDao extends BaseDao<ServiceConnectionEntity> {
}
