-- Hashes only: device bootstrap secrets are displayed once, never stored.
CREATE TABLE ai_native_device_grant (
    device_id VARCHAR(64) NOT NULL PRIMARY KEY,
    owner_id BIGINT NOT NULL,
    connection_id VARCHAR(36) NOT NULL,
    connection_revision INT NOT NULL,
    token_hash CHAR(64) NOT NULL,
    client_id VARCHAR(128) NOT NULL,
    expires_at BIGINT NOT NULL,
    revoked BOOLEAN NOT NULL DEFAULT FALSE,
    original_ota_url VARCHAR(1024) NOT NULL DEFAULT '',
    INDEX idx_native_grant_hash (token_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
