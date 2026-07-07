"""Full-pipeline integration test with the external APIs stubbed out."""
import pytest

from legomon.models import Listing
from legomon.pipeline import Pipeline


@pytest.fixture
def pipeline(config, store, monkeypatch):
    config.data["storage"]["download_photos"] = False
    pipe = Pipeline(config, store)
    # a validated set worth £100 used / £150 new
    store.save_set("75257-1", "Millennium Falcon", "Star Wars", 2019, 1351, 159.99)
    monkeypatch.setattr(
        pipe.identifier, "identify",
        lambda lid: [{"set_number": "75257-1", "confidence": 0.95, "quantity": 1}])
    monkeypatch.setattr(
        pipe.pricer, "market_value",
        lambda num, cond: {"value": 150.0 if cond == "new" else 100.0,
                           "n_sales": 12, "source": "stub", "sanity_check": None})
    def fake_assess(lid):
        store.save_condition(lid, "boxed-complete", [], 0.9, "")
        return {"condition": "boxed-complete", "flags": [],
                "completeness_confidence": 0.9, "notes": ""}
    monkeypatch.setattr(pipe.assessor, "assess", fake_assess)
    return pipe


def make_listing(price, sid="42"):
    return Listing(source="gumtree", source_id=sid, url=f"https://g/{sid}",
                   title="LEGO 75257 falcon", price=price)


def test_deal_scored_and_alerted(pipeline, store, config, monkeypatch):
    sent = []
    monkeypatch.setattr("legomon.pipeline.send_deal_alert",
                        lambda cfg, listing, score, cond, sets:
                        sent.append(listing["id"]) or ["ntfy"])
    [lid] = pipeline.ingest([make_listing(price=40.0)])
    score = pipeline.process_listing(lid)
    # used £100 x mult 1.0 x (1-0.13) = £87 net; ask £40 → deal
    assert score["estimated_net"] == 87.0
    assert score["is_deal"]
    assert sent == [lid]
    assert store.was_alerted(lid, "ntfy")
    # re-processing must not re-alert
    pipeline.process_listing(lid)
    assert sent == [lid]


def test_dry_run_sends_nothing(pipeline, store, monkeypatch):
    monkeypatch.setattr("legomon.pipeline.send_deal_alert",
                        lambda *a: pytest.fail("alert sent during dry run"))
    [lid] = pipeline.ingest([make_listing(price=40.0, sid="43")])
    score = pipeline.process_listing(lid, dry_run=True)
    assert score["is_deal"]
    assert not store.was_alerted(lid, "ntfy")


def test_fair_price_not_alerted(pipeline, store, monkeypatch):
    monkeypatch.setattr("legomon.pipeline.send_deal_alert",
                        lambda *a: pytest.fail("alert sent for non-deal"))
    [lid] = pipeline.ingest([make_listing(price=80.0, sid="44")])
    score = pipeline.process_listing(lid)
    assert not score["is_deal"]


def test_report_shows_scored(pipeline, store):
    [lid] = pipeline.ingest([make_listing(price=40.0, sid="45")])
    pipeline.process_listing(lid, dry_run=True)
    rows = store.top_scored(20)
    assert len(rows) == 1
    assert rows[0]["margin"] == 47.0
    assert rows[0]["condition"] == "boxed-complete"
