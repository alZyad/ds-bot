from types import SimpleNamespace

from discord.opus import OpusError

from dsbot import dave


class Session:
    ready = True

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def decrypt(self, user_id, media_type, data):
        self.calls.append((user_id, media_type, data))
        if self.fail:
            raise ValueError("Failed to decrypt: NoDecryptorForUser")
        return b"plain:" + data


def voice(session, *, version=1, ssrc_map=None):
    return SimpleNamespace(
        _connection=SimpleNamespace(dave_session=session, dave_protocol_version=version),
        _get_id_from_ssrc=lambda ssrc: (ssrc_map or {7: 42}).get(ssrc),
    )


def test_frames_are_decrypted_for_their_sender():
    session = Session()
    assert dave.dave_decrypt(voice(session), 7, b"x") == b"plain:x"
    assert session.calls == [(42, dave.davey.MediaType.audio, b"x")]


def test_without_dave_frames_pass_through():
    assert dave.dave_decrypt(voice(None), 7, b"x") == b"x"
    assert dave.dave_decrypt(voice(Session(), version=0), 7, b"x") == b"x"


def test_undecryptable_frames_become_empty():
    assert dave.dave_decrypt(voice(Session(fail=True)), 7, b"x") == b""
    assert dave.dave_decrypt(voice(Session()), 8, b"x") == b""


def test_a_session_not_ready_to_send_still_decrypts():
    listening = Session()
    listening.ready = False
    assert dave.dave_decrypt(voice(listening), 7, b"x") == b"plain:x"


def test_install_wraps_the_reader_decryptor_once():
    inner = SimpleNamespace(decrypt_rtp=lambda packet: b"x", update_secret_key=lambda key: key)
    client = voice(Session())
    client._reader = SimpleNamespace(decryptor=inner)
    dave.install(client)
    dave.install(client)
    wrapped = client._reader.decryptor
    assert isinstance(wrapped, dave.DaveDecryptor) and wrapped._inner is inner
    assert wrapped.decrypt_rtp(SimpleNamespace(ssrc=7)) == b"plain:x"
    assert wrapped.update_secret_key(b"k") == b"k"


def test_a_corrupt_frame_decodes_to_silence_instead_of_crashing():
    class Broken:
        def decode(self, data, fec=False):
            raise OpusError.__new__(OpusError)

    class Packet:
        decrypted_data = b"garbage"

    decoder = SimpleNamespace(_decoder=Broken())
    packet = Packet()
    _, pcm = dave.PacketDecoder._decode_packet(decoder, packet)
    assert pcm == dave.SILENT_FRAME


def test_describe_reports_the_session_state():
    assert dave.describe(voice(None)) == "version=1 no session"
    session = Session()
    session.status, session.epoch = "active", 3
    session.get_user_ids = lambda: ["42"]
    assert dave.describe(voice(session)) == "version=1 status=active ready=True epoch=3 users=['42']"
