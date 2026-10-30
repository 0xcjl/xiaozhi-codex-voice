-- Connection metadata only. Provider credentials remain in existing model config / runtime.
CREATE TABLE ai_service_connection (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name VARCHAR(80) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    model_id VARCHAR(64) NULL,
    runtime_id VARCHAR(64) NULL,
    model VARCHAR(80) NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    revision INT NOT NULL DEFAULT 1,
    INDEX idx_service_connection_owner (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE ai_device_connection (
    device_id VARCHAR(64) NOT NULL PRIMARY KEY,
    connection_id VARCHAR(36) NOT NULL,
    INDEX idx_device_connection_target (connection_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
