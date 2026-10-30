import unittest
from core.providers.realtime.task_announcements import report_matches,normalize_numbers,speech_text


class TaskAnnouncementFactsTests(unittest.TestCase):
    def test_file_link_targets_do_not_turn_into_spoken_amounts_or_ids(self):
        text=speech_text('Wrote `247` to [answer.txt](/Users/test/2026/task-123/answer.txt).')
        self.assertEqual(text,'Wrote 247 to answer.txt.')
    def test_chinese_amounts_and_label_match_same_report(self):
        self.assertTrue(report_matches('灯塔一号','总额 243，数据记录 2 条。','任务代号灯塔一号，总额二百四十三，数据记录两条。'))
    def test_wrong_amount_or_other_task_label_cannot_pass(self):
        self.assertFalse(report_matches('灯塔一号','总额 35','任务代号灯塔一号，总额五十。'))
        self.assertFalse(report_matches('灯塔一号','总额 35','灯塔一号，竹林一号，总额三十五。',['灯塔一号','竹林一号']))
    def test_label_number_cannot_masquerade_as_result(self):
        self.assertFalse(report_matches('灯塔一号','结果 1','任务代号灯塔一号。'))
    def test_decimal_amount_normalization(self):
        self.assertEqual(normalize_numbers('二百零三点五'),'203.5')
        self.assertTrue(report_matches('白帆一号','金额 203.5','白帆一号，金额二百零三点五。'))
    def test_negative_amount_cannot_be_read_as_positive(self):
        self.assertFalse(report_matches('白帆一号','金额 -35','白帆一号，金额三十五。'))
        self.assertTrue(report_matches('白帆一号','金额 -35.0','白帆一号，金额负三十五。'))
    def test_native_english_digit_sequences_are_the_same_verified_value(self):
        self.assertTrue(report_matches('竹林一号','Wrote 368 to answer.txt','任务代号竹林一号。Wrote three six eight to answer dot txt.'))
        self.assertFalse(report_matches('竹林一号','Wrote 368','竹林一号。Wrote three six nine.'))
    def test_english_normal_numbers_and_decimals(self):
        self.assertEqual(normalize_numbers('two hundred and forty-seven'),'247')
        self.assertEqual(normalize_numbers('one and two'),'1 and 2')
        self.assertEqual(normalize_numbers('negative two point five'),'-2.5')
        self.assertEqual(normalize_numbers('three hundred sixty eight and two hundred forty seven'),'368 and 247')

    def test_percentage_word_does_not_invent_an_extra_hundred(self):
        self.assertTrue(report_matches('','音量46%。','音量百分之四十六。'))
        self.assertFalse(report_matches('','音量46%。','音量百分之四十六，或者百分之六十。'))
    def test_extra_numeric_claims_are_not_accepted(self):
        self.assertFalse(report_matches('灯塔一号','总额 35','任务灯塔一号，总额35，也可能50。'))
    def test_model_hyphen_is_not_a_negative_amount_and_model_suffix_must_match(self):
        self.assertTrue(report_matches('','对话后台 gpt-6.1-terra','对话后台 GPT 六点一 Terra。'))
        self.assertFalse(report_matches('','对话后台 gpt-6.1-terra','对话后台 GPT 六点一 SOL。'))


    def test_model_name_adjacent_to_chinese_text_is_still_verified(self):
        self.assertTrue(report_matches('','对话后台gpt-6.1-terra。','Harbor; gpt-6.1-terra.'))
        self.assertFalse(report_matches('','对话后台gpt-6.1-terra。','gpt-6.1-SOL.'))

    def test_repeated_same_verified_model_is_allowed_but_added_model_is_not(self):
        self.assertTrue(report_matches('','对话后台 gpt-6.1-terra','对话后台 GPT 六点一 Terra。任务也由 GPT 六点一 Terra 执行。'))
        self.assertFalse(report_matches('','对话后台 gpt-6.1-terra','对话后台 GPT 六点一 Terra，也可能 GPT 六点一 SOL。'))


class DeviceControlAudioGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_unverified_native_control_ack_is_not_played_or_buffered(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock,Mock
        from core.providers.realtime.task_announcements import TaskAnnouncements
        playback=SimpleNamespace(feed=AsyncMock())
        connection=SimpleNamespace(device_control_pending=True,playback=playback)
        binding=SimpleNamespace(store=Mock())
        announcements=TaskAnnouncements(connection,binding)
        pcm=(4000).to_bytes(2,'little',signed=True)*320
        await announcements.feed(pcm)
        playback.feed.assert_not_awaited();self.assertEqual(announcements.buffer,bytearray())
        # Verified report generation still reaches the existing fact-check buffer.
        announcements.capture=True
        await announcements.feed(pcm)
        self.assertEqual(bytes(announcements.buffer),pcm)
        announcements.capture=False;connection.device_control_pending=False
        await announcements.feed(pcm)
        playback.feed.assert_awaited_once_with(pcm)
