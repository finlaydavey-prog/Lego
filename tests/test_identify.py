from legomon.identify import Identifier, _parse_json_reply


def make_identifier(config, store):
    return Identifier(config, store)


def test_candidates_basic(config, store):
    ident = make_identifier(config, store)
    nums = ident.candidates_from_text(
        "LEGO Star Wars 75257 Millennium Falcon", "great condition")
    assert nums == ["75257"]


def test_candidates_skip_piece_counts(config, store):
    ident = make_identifier(config, store)
    nums = ident.candidates_from_text("Huge lego set", "over 1500 pieces included")
    assert "1500" not in nums


def test_candidates_deprioritise_years(config, store):
    ident = make_identifier(config, store)
    nums = ident.candidates_from_text(
        "LEGO 10294 Titanic from 2021", "")
    assert nums[0] == "10294"
    assert "2021" in nums  # kept, but behind — Rebrickable will reject it


def test_candidates_multiple_sets(config, store):
    ident = make_identifier(config, store)
    nums = ident.candidates_from_text(
        "Job lot: 75192, 10294 and 21318 treehouse", "")
    assert set(nums) >= {"75192", "10294", "21318"}


def test_parse_json_reply_handles_wrapping():
    assert _parse_json_reply('Here you go:\n{"sets": []} thanks!') == {"sets": []}
    assert _parse_json_reply("no json here") is None


def test_lookup_uses_negative_cache(config, store, monkeypatch):
    ident = make_identifier(config, store)
    store.save_set("99999-1", "", "", None, None, None)  # negative-cached
    calls = []
    monkeypatch.setattr("requests.get", lambda *a, **k: calls.append(1))
    assert ident.lookup_set("99999") is None
    assert calls == []  # no network hit
