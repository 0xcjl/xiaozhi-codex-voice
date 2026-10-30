"""Experimental PCM/Opus conversion for the realtime provider.

The caller must select parameters from the actual device handshake and apply
protocol framing, pacing, cancellation and authentication separately.
"""

import opuslib_next


class DeviceAudioCodec:
    def __init__(self, input_rate=16000, output_rate=16000, frame_ms=60):
        if input_rate not in (8000, 12000, 16000, 24000, 48000):
            raise ValueError("unsupported Opus input rate")
        if output_rate not in (8000, 12000, 16000, 24000, 48000):
            raise ValueError("unsupported Opus output rate")
        if frame_ms not in (10, 20, 40, 60):
            raise ValueError("unsupported frame duration")
        self.input_samples = input_rate * frame_ms // 1000
        self.output_samples = output_rate * frame_ms // 1000
        self.output_frame_bytes = self.output_samples * 2
        self.output_rate = output_rate
        self.decoder = opuslib_next.Decoder(input_rate, 1)
        self.encoder = opuslib_next.Encoder(output_rate, 1, opuslib_next.APPLICATION_AUDIO)
        self.output_buffer = bytearray()

    def decode_input(self, packet: bytes) -> bytes:
        if not packet or len(packet) > 8192:
            raise ValueError("invalid input packet size")
        return self.decoder.decode(packet, self.input_samples)

    def encode_output(self, pcm: bytes) -> list[bytes]:
        if len(pcm) % 2:
            raise ValueError("PCM must contain 16-bit samples")
        if len(self.output_buffer) + len(pcm) > self.output_frame_bytes * 16:
            raise ValueError("output buffer limit exceeded")
        self.output_buffer.extend(pcm)
        packets = []
        while len(self.output_buffer) >= self.output_frame_bytes:
            frame = bytes(self.output_buffer[:self.output_frame_bytes])
            del self.output_buffer[:self.output_frame_bytes]
            packets.append(self.encoder.encode(frame, self.output_samples))
        return packets

    def discard_output(self):
        """Drop unsent samples and reset the encoder after an interruption."""
        self.output_buffer.clear()
        self.encoder = opuslib_next.Encoder(self.output_rate, 1, opuslib_next.APPLICATION_AUDIO)

    def finish_output(self):
        """Pad the last partial frame with silence and encode it once."""
        if not self.output_buffer:
            return None
        frame = bytes(self.output_buffer) + bytes(self.output_frame_bytes - len(self.output_buffer))
        self.output_buffer.clear()
        return self.encoder.encode(frame, self.output_samples)
