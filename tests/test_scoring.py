from legomon.scoring import Scorer


def test_score_math(config):
    s = Scorer(config)
    # market £100, sealed (mult 1.0), fees 13% -> net £87
    score = s.score(asking_price=40, market_value=100, condition="sealed", flags=[])
    assert score["estimated_net"] == 87.0
    assert score["margin"] == 47.0
    assert abs(score["margin_pct"] - 1.175) < 0.001
    # 40 <= 0.6 * 87 = 52.2 and margin >= 10 -> deal
    assert score["is_deal"]


def test_not_a_deal_above_ratio(config):
    s = Scorer(config)
    score = s.score(asking_price=80, market_value=100, condition="sealed", flags=[])
    assert not score["is_deal"]


def test_flags_reduce_multiplier(config):
    s = Scorer(config)
    base = s.condition_multiplier("boxed-complete", [])
    flagged = s.condition_multiplier("boxed-complete",
                                     ["missing_minifigures", "yellowing"])
    assert flagged < base
    assert flagged >= s.min_multiplier


def test_multiplier_floor(config):
    s = Scorer(config)
    many = ["yellowing", "sun_damage", "damaged_box", "missing_minifigures",
            "knockoff_bricks", "smoke_pet", "stickers_applied"]
    assert s.condition_multiplier("unknown", many) == s.min_multiplier


def test_price_column_mapping(config):
    s = Scorer(config)
    assert s.price_column("sealed") == "new"
    for c in ("boxed-complete", "boxed-incomplete", "built-complete",
              "loose-bulk", "unknown"):
        assert s.price_column(c) == "used"


def test_unpriceable_returns_none(config):
    s = Scorer(config)
    assert s.score(None, 100, "sealed", []) is None
    assert s.score(10, None, "sealed", []) is None
    assert s.score(0, 100, "sealed", []) is None
