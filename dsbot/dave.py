from __future__ import annotations

import logging

from discord.ext.voice_recv.opus import PacketDecoder
from discord.opus import Decoder, OpusError

try:
    import davey
except ImportError:
    davey = None

log = logging.getLogger(__name__)

SILENT_FRAME = b"\x00" * Decoder.FRAME_SIZE


def dave_decrypt(voice_client, ssrc: int, data: bytes) -> bytes:
    state = getattr(voice_client, "_connection", None)
    session = getattr(state, "dave_session", None)
    if davey is None or session is None or not getattr(state, "dave_protocol_version", 0):
        return data
    user_id = voice_client._get_id_from_ssrc(ssrc)
    if user_id is None or not session.ready:
        return b""
    try:
        return session.decrypt(user_id, davey.MediaType.audio, data) or b""
    except ValueError as exc:
        log.debug("DAVE decrypt failed for ssrc %s: %s", ssrc, exc)
        return b""


class DaveDecryptor:
    def __init__(self, inner, voice_client) -> None:
        self._inner = inner
        self._voice_client = voice_client

    def decrypt_rtp(self, packet) -> bytes:
        return dave_decrypt(self._voice_client, packet.ssrc, self._inner.decrypt_rtp(packet))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install(voice_client) -> None:
    reader = voice_client._reader
    if not isinstance(reader.decryptor, DaveDecryptor):
        reader.decryptor = DaveDecryptor(reader.decryptor, voice_client)


_decode_packet = PacketDecoder._decode_packet


def _decode_or_silence(self, packet):
    try:
        return _decode_packet(self, packet)
    except OpusError as exc:
        log.debug("dropping undecodable opus frame: %s", exc)
        return packet, SILENT_FRAME


PacketDecoder._decode_packet = _decode_or_silence
