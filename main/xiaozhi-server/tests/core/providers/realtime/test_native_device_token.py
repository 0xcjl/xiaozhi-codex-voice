import unittest
from unittest.mock import patch
from core.auth import AuthManager

class NativeDeviceTokenTests(unittest.TestCase):
    def setUp(self):
        self.auth=AuthManager('synthetic-test-server-secret')
    def test_native_token_is_scoped_to_owner_connection_client_and_device(self):
        token=self.auth.generate_native_token('client','02:00:00:00:00:01','123','connection')
        self.assertTrue(self.auth.verify_native_token(token,'client','02:00:00:00:00:01','123','connection'))
        for args in [('other','02:00:00:00:00:01','123','connection'),('client','02:00:00:00:00:02','123','connection'),('client','02:00:00:00:00:01','999','connection'),('client','02:00:00:00:00:01','123','other')]:
            self.assertFalse(self.auth.verify_native_token(token,*args))
        self.assertFalse(self.auth.verify_token(token,'client','02:00:00:00:00:01'))
    def test_cross_language_fixed_vector(self):
        with patch('core.auth.time.time',return_value=1000):
            token=self.auth.generate_native_token('client','02:00:00:00:00:01','123','connection')
        self.assertEqual(token,'native1.Po_eflAyR-EMzHoHCHerr9P7rcMxFFRgPxJz5a0JH6A.1000')
    def test_generic_ota_token_can_never_authorize_native_voice(self):
        old=self.auth.generate_token('client','02:00:00:00:00:01')
        self.assertFalse(self.auth.verify_native_token(old,'client','02:00:00:00:00:01','123','connection'))
    def test_expired_future_and_tampered_native_tokens_are_rejected(self):
        with patch('core.auth.time.time',return_value=1000):
            token=self.auth.generate_native_token('client','mac','123','connection')
        with patch('core.auth.time.time',return_value=1901):
            self.assertFalse(self.auth.verify_native_token(token,'client','mac','123','connection'))
        with patch('core.auth.time.time',return_value=900):
            self.assertFalse(self.auth.verify_native_token(token,'client','mac','123','connection'))
        self.assertFalse(self.auth.verify_native_token('native1.bad.1','client','mac','123','connection'))
