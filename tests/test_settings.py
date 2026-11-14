from dsbot.settings import Settings


def test_opt_in_persists_per_server(tmp_path):
    path = tmp_path / "settings.json"
    first = Settings(path)
    assert first.opted_in(100) == []

    assert first.set_opted_in(100, 7, True) is True
    assert first.set_opted_in(100, 7, True) is False
    first.set_opted_in(100, 8, True)
    assert Settings(path).opted_in(100) == [7, 8]
    assert Settings(path).is_opted_in(100, 7)
    assert not Settings(path).is_opted_in(200, 7)

    first.set_opted_in(100, 7, False)
    first.set_opted_in(100, 8, False)
    assert Settings(path).opted_in(100) == []
    assert Settings(path).guild_option(100, "opted_in") is None


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
    assert settings.opted_in(1) == []
    settings.set_opted_in(1, 5, True)
    assert Settings(path).opted_in(1) == [5]


def test_missing_file_is_not_an_error(tmp_path):
    settings = Settings(tmp_path / "nested" / "settings.json")
    assert settings.name_for(1) is None
    settings.remember_name(1, "x")
    assert (tmp_path / "nested" / "settings.json").exists()
