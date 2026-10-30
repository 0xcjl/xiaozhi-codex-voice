import unittest
from core.providers.realtime.task_announcements import VerifiedAudioCache

class VerifiedAudioCacheTests(unittest.TestCase):
    def test_cache_is_exact_and_separated_by_owner_scope_and_model(self):
        cache=VerifiedAudioCache();key=('owner-device','gpt-6.1-terra','音量46%。')
        cache.put(key,b'\x01\x00'*100,now=10)
        self.assertIsNotNone(cache.get(key,now=11))
        for other in (('foreign','gpt-6.1-terra','音量46%。'),('owner-device','other-model','音量46%。'),('owner-device','gpt-6.1-terra','音量62%。')):
            self.assertIsNone(cache.get(other,now=11))
        self.assertIsNone(cache.get(key,now=311))
    def test_cache_has_memory_and_entry_limits(self):
        cache=VerifiedAudioCache(max_bytes=12,max_entries=2)
        for value in range(3):cache.put(('s','m',str(value)),b'123456',now=value)
        self.assertLessEqual(cache.size,12);self.assertIsNone(cache.get(('s','m','0'),now=3))
        self.assertIsNotNone(cache.get(('s','m','2'),now=3))
