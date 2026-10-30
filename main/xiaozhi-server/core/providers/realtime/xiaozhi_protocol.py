"""Preparation only: XiaoZhi WebSocket audio framing, no server or device I/O.

Based on 78/xiaozhi-esp32 v2.0.5. The attached device's actual transport and
build options have not been observed, and this module is not a voice bridge.
"""

from dataclasses import dataclass
from struct import pack, unpack_from

MAX_AUDIO_PACKET_BYTES = 8192  # Bridge-side bound; not an Opus format limit.


@dataclass(frozen=True)
class AudioPacket:
    opus: bytes
    timestamp_ms: int = 0


def parse_hello(message: dict) -> tuple[int, int, int]:
    """Read version, sample rate and frame duration from a WebSocket hello."""
    if message.get("type") != "hello" or message.get("transport") != "websocket":
        raise ValueError("expected a WebSocket hello")
    version = message.get("version")
    audio = message.get("audio_params")
    if type(version) is not int or version not in (1, 2, 3) or not isinstance(audio, dict):
        raise ValueError("unsupported protocol version or audio parameters")
    rate = audio.get("sample_rate")
    duration = audio.get("frame_duration")
    if audio.get("format") != "opus" or audio.get("channels") != 1:
        raise ValueError("only mono Opus is supported")
    if type(rate) is not int or rate not in (8000, 12000, 16000, 24000, 48000):
        raise ValueError("unsupported sample rate")
    if type(duration) is not int or duration not in (10, 20, 40, 60):
        raise ValueError("unsupported frame duration")
    return version, rate, duration


def unpack_audio(version: int, frame: bytes) -> AudioPacket:
    """Extract Opus data from version 1, 2, or 3 binary framing."""
    if version == 1:
        packet = AudioPacket(frame)
    elif version == 2:
        if len(frame) < 16:
            raise ValueError("short v2 frame")
        encoded_version, kind, reserved, timestamp, size = unpack_from("!HHIII", frame)
        if encoded_version != 2 or kind != 0 or reserved != 0 or size != len(frame) - 16:
            raise ValueError("invalid v2 audio frame")
        packet = AudioPacket(frame[16:], timestamp)
    elif version == 3:
        if len(frame) < 4:
            raise ValueError("short v3 frame")
        kind, reserved, size = unpack_from("!BBH", frame)
        if kind != 0 or reserved != 0 or size != len(frame) - 4:
            raise ValueError("invalid v3 audio frame")
        packet = AudioPacket(frame[4:])
    else:
        raise ValueError("unsupported protocol version")
    if not 1 <= len(packet.opus) <= MAX_AUDIO_PACKET_BYTES:
        raise ValueError("invalid Opus packet size")
    return packet


def pack_audio(version: int, packet: AudioPacket) -> bytes:
    """Wrap an already encoded Opus packet for the selected protocol."""
    if not 1 <= len(packet.opus) <= MAX_AUDIO_PACKET_BYTES:
        raise ValueError("invalid Opus packet size")
    if version == 1:
        return packet.opus
    if version == 2:
        if not 0 <= packet.timestamp_ms <= 0xFFFFFFFF:
            raise ValueError("invalid timestamp")
        return pack("!HHIII", 2, 0, 0, packet.timestamp_ms, len(packet.opus)) + packet.opus
    if version == 3:
        return pack("!BBH", 0, 0, len(packet.opus)) + packet.opus
    raise ValueError("unsupported protocol version")
