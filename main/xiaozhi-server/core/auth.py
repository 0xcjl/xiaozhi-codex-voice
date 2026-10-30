import hmac
import base64
import hashlib
import time


class AuthenticationError(Exception):
    """认证异常"""

    pass


class AuthManager:
    """
    统一授权认证管理器
    生成与验证 client_id device_id token（HMAC-SHA256）认证三元组
    token 中不含明文 client_id/device_id，只携带签名 + 时间戳; client_id/device_id在连接时传递
    在 MQTT 中 client_id: client_id, username: device_id, password: token
    在 Websocket 中，header:{Device-ID: device_id, Client-ID: client_id, Authorization: Bearer token, ......}
    """

    def __init__(self, secret_key: str, expire_seconds: int = 60 * 60 * 24 * 30):
        if not expire_seconds or expire_seconds < 0:
            self.expire_seconds = 60 * 60 * 24 * 30
        else:
            self.expire_seconds = expire_seconds
        self.secret_key = secret_key

    def _sign(self, content: str) -> str:
        """HMAC-SHA256签名并Base64编码"""
        sig = hmac.new(
            self.secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """
        生成 token
        Args:
            client_id: 设备连接ID
            username: 设备用户名（通常为deviceId）
        Returns:
            str: token字符串
        """
        ts = int(time.time())
        content = f"{client_id}|{username}|{ts}"
        signature = self._sign(content)
        # token仅包含签名与时间戳，不包含明文信息
        token = f"{signature}.{ts}"
        return token

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """
        验证token有效性
        Args:
            token: 客户端传入的token
            client_id: 连接使用的client_id
            username: 连接使用的username
        """
        try:
            sig_part, ts_str = token.split(".")
            ts = int(ts_str)
            if int(time.time()) - ts > self.expire_seconds:
                return False  # 过期

            expected_sig = self._sign(f"{client_id}|{username}|{ts}")
            if not hmac.compare_digest(sig_part, expected_sig):
                return False

            return True
        except Exception:
            return False

    def _native_signature(self, client_id, username, owner_id, connection_id, timestamp):
        scope = f"xiaozhi-native-v1|{owner_id}|{connection_id}".encode()
        key = hmac.new(self.secret_key.encode(), scope, hashlib.sha256).digest()
        content = f"{client_id}|{username.lower()}|{timestamp}".encode()
        return base64.urlsafe_b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode().rstrip("=")

    def generate_native_token(self, client_id, username, owner_id, connection_id):
        timestamp = int(time.time())
        signature = self._native_signature(client_id, username, owner_id, connection_id, timestamp)
        return f"native1.{signature}.{timestamp}"

    def verify_native_token(self, token, client_id, username, owner_id, connection_id):
        """Native voice accepts only a short-lived, owner-issued connection token.

        Generic tokens returned by anonymous upstream OTA can never authorize it.
        The expiry is checked on connection; it does not revoke an active session.
        """
        try:
            prefix, signature, timestamp_text = token.split(".")
            timestamp = int(timestamp_text)
            age = int(time.time()) - timestamp
            if prefix != "native1" or str(timestamp) != timestamp_text or age < -60 or age > 900:
                return False
            expected = self._native_signature(client_id, username, owner_id, connection_id, timestamp)
            return hmac.compare_digest(signature, expected)
        except (ValueError, TypeError, AttributeError):
            return False
