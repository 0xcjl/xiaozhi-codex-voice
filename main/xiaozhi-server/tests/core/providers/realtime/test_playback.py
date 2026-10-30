import unittest

from core.providers.realtime.playback import DevicePlayback


class DevicePlaybackTests(unittest.IsolatedAsyncioTestCase):
    def make_playback(self):
        events = []
        class Device:
            output_active = False
            async def output_start(self):
                self.output_active = True
                events.append("start")
            async def output_finish(self):
                self.output_active = False
                events.append("stop")
        class Pipe:
            async def _output_pcm(self, pcm):
                events.append(pcm)
        device = Device()
        return DevicePlayback(device, Pipe()), device, events

    async def test_audio_starts_without_transcript_and_silence_finishes(self):
        playback, _, events = self.make_playback()
        playback.ready = True
        silence = bytes(640)
        speech = b"\x00\x10" * 320
        await playback.feed(silence)
        self.assertEqual(events, [])
        await playback.feed(speech)
        self.assertEqual(events[:2], ["start", silence + speech])
        for _ in range(40):
            await playback.feed(silence)
        self.assertEqual(events[-1], "stop")

    async def test_preroll_bounded_and_no_output_before_ready(self):
        playback, _, events = self.make_playback()
        await playback.feed(b"\x00\x10" * 320)
        self.assertEqual(events, [])
        playback.ready = True
        for _ in range(100):
            await playback.feed(bytes(640))
        self.assertLessEqual(len(playback.preroll), 5120)

    async def test_abort_discards_old_audio_until_quiet_gap(self):
        playback, device, events = self.make_playback()
        playback.ready = True
        playback.block()
        for _ in range(20):
            await playback.feed(b"\x00\x10" * 320)
        self.assertEqual(events, [])
        for _ in range(10):
            await playback.feed(bytes(640))
        await playback.feed(b"\x00\x10" * 320)
        self.assertTrue(device.output_active)
        self.assertEqual(events[0], "start")



class PlaybackSourceGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_without_silent_tail_still_emits_stop(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from core.providers.realtime.playback import DevicePlayback
        device=SimpleNamespace(output_active=True,output_finish=AsyncMock())
        playback=DevicePlayback(device,SimpleNamespace());playback.last_pcm_at=10
        await playback.finish_if_idle(11.19);device.output_finish.assert_not_awaited()
        await playback.finish_if_idle(11.21);device.output_finish.assert_awaited_once()
    async def test_no_idle_flush_while_audio_is_still_arriving(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from core.providers.realtime.playback import DevicePlayback
        device=SimpleNamespace(output_active=True,output_finish=AsyncMock())
        playback=DevicePlayback(device,SimpleNamespace());playback.last_pcm_at=12
        await playback.finish_if_idle(12.8);device.output_finish.assert_not_awaited()
