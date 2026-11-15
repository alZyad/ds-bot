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
_reported: set[tuple] = set()


def _report_once(key: tuple, message: str, *args) -> None:
    if key not in _reported:
        _reported.add(key)
        log.info(message, *args)


def dave_decrypt(voice_client, ssrc: int, data: bytes) -> bytes:
    state = getattr(voice_client, "_connection", None)
    session = getattr(state, "dave_session", None)
    if davey is None or session is None or not getattr(state, "dave_protocol_version", 0):
        return data
    user_id = voice_client._get_id_from_ssrc(ssrc)
    if user_id is None:
        _report_once(("unknown", ssrc), "DAVE: audio from unmapped ssrc %s dropped", ssrc)
        return b""
    try:
        plain = session.decrypt(user_id, davey.MediaType.audio, data) or b""
    except ValueError as exc:
        _report_once(("failed", user_id, str(exc)), "DAVE: cannot decrypt %s: %s", user_id, exc)
        return b""
    _report_once(("ok", user_id), "DAVE: decrypting voice of %s", user_id)
    return plain


def describe(voice_client) -> str:
    state = getattr(voice_client, "_connection", None)
    session = getattr(state, "dave_session", None)
    version = getattr(state, "dave_protocol_version", 0)
    if session is None:
        return f"version={version} no session"
    return (
        f"version={version} status={session.status} ready={session.ready} "
        f"epoch={session.epoch} users={session.get_user_ids()}"
    )


class DaveDecryptor:
    def __init__(self, inner, voice_client) -> None:
        self._inner = inner
        self._voice_client = voice_client

    def decrypt_rtp(self, packet) -> bytes:
        return dave_decrypt(self._voice_client, packet.ssrc, self._inner.decrypt_rtp(packet))

    def __getattr__(self, name):
        return getattr(self._inner, name)


def install(voice_client) -> None:
    _reported.clear()
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
