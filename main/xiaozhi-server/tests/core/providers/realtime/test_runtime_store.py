import tempfile
import unittest
from pathlib import Path
from core.providers.realtime.runtime_store import RuntimeStore


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RuntimeStore(Path(self.temp.name)/'private')
    def test_thread_and_bounded_history_survive_reopen(self):
        self.store.set_thread('scope','thread-one')
        for i in range(40):self.store.remember('scope',str(i),'user',f'question {i}')
        again=RuntimeStore(self.store.root)
        self.assertEqual(again.thread('scope'),'thread-one')
        self.assertEqual(len(again.history('scope')),32)
        self.assertEqual(again.history('other'),[])
    def test_duplicate_request_never_creates_another_task(self):
        a=self.store.create_task('scope','call-one','calculate')
        b=self.store.create_task('scope','call-one','calculate')
        self.assertEqual(a['id'],b['id']);self.assertEqual(len(self.store.tasks('scope')),1)
        self.assertNotEqual(a['id'],self.store.create_task('other','call-one','calculate')['id'])
    def test_foreign_task_is_not_readable_and_terminal_result_is_immutable(self):
        job=self.store.create_task('scope','call','calculate')
        self.assertIsNone(self.store.task('other',job['id']))
        self.store.finish_task('scope',job['id'],'completed','result 91')
        self.store.finish_task('scope',job['id'],'failed','wrong')
        self.assertEqual(self.store.task('scope',job['id'])['result'],'result 91')
        self.assertEqual(self.store.task('scope',job['label'])['id'],job['id'])
        self.assertIsNone(self.store.task('other',job['label']))
    def test_late_old_delivery_cannot_acknowledge_reclaimed_event(self):
        job=self.store.create_task('scope','call','calculate')
        first=self.store.claim_event('scope','old')
        self.store.release_claims('scope','old')
        second=self.store.claim_event('scope','new')
        self.assertEqual(first['id'],second['id'])
        self.assertFalse(self.store.finish_event('scope',first['id'],'old'))
        self.assertTrue(self.store.finish_event('scope',second['id'],'new'))
    def test_process_recovery_marks_work_interrupted_not_completed(self):
        job=self.store.create_task('scope','call','calculate');self.store.start_task('scope',job['id'])
        self.store.recover()
        self.assertEqual(self.store.task('scope',job['id'])['state'],'interrupted')
        self.assertNotEqual(self.store.task('scope',job['id'])['result'],'completed')
