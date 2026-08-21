from dsbot.settings import Settings


def test_channel_overrides_persist(tmp_path):
    path = tmp_path / "settings.json"
    first = Settings(path)
    assert first.silence_rms(7, default=150) == 150

    first.set_silence_rms(7, 320)
    assert Settings(path).silence_rms(7, default=150) == 320
    assert Settings(path).silence_rms(8, default=150) == 150

    first.set_silence_rms(7, None)  # back to the default
    assert Settings(path).silence_rms(7, default=150) == 150
    assert Settings(path).channel_option(7, "silence_rms") is None


def test_autojoin_persists(tmp_path):
    path = tmp_path / "settings.json"
    first = Settings(path)
    assert first.autojoin(100, default=False) is False
    assert first.autojoin(100, default=True) is True

    first.set_autojoin(100, True)
    assert Settings(path).autojoin(100, default=False) is True

    first.set_autojoin(100, None)
    assert Settings(path).autojoin(100, default=False) is False
    assert Settings(path).guild_option(100, "autojoin") is None


def test_names_survive_a_restart(tmp_path):
    path = tmp_path / "settings.json"
    settings = Settings(path)
    settings.remember_name(42, "Zyad (@zyad)")
    settings.remember_name(42, "Zyad (@zyad)")  # idempotent
    settings.remember_name(43, "")              # ignored

    reloaded = Settings(path)
    assert reloaded.name_for(42) == "Zyad (@zyad)"
    assert reloaded.name_for(43) is None
    assert reloaded.name_for(99) is None


def test_a_corrupt_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    settings = Settings(path)
    assert settings.silence_rms(1, default=150) == 150
    # and it recovers by rewriting the file
    settings.set_silence_rms(1, 200)
    assert Settings(path).silence_rms(1, default=150) == 200


def test_missing_file_is_not_an_error(tmp_path):
    settings = Settings(tmp_path / "nested" / "settings.json")
    assert settings.name_for(1) is None
    settings.remember_name(1, "x")
    assert (tmp_path / "nested" / "settings.json").exists()
