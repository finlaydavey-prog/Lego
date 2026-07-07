from legomon.models import Listing


def make_listing(**kw):
    base = dict(source="gumtree", source_id="123", url="https://x/123",
                title="LEGO 75192", price=50.0)
    base.update(kw)
    return Listing(**base)


def test_upsert_dedupes(store):
    lid1, new1, _ = store.upsert_listing(make_listing())
    lid2, new2, changed2 = store.upsert_listing(make_listing())
    assert lid1 == lid2
    assert new1 and not new2 and not changed2


def test_price_change_tracked_and_requeued(store):
    lid, _, _ = store.upsert_listing(make_listing(price=50.0))
    store.set_status(lid, "scored")
    _, is_new, changed = store.upsert_listing(make_listing(price=40.0))
    assert not is_new and changed
    row = store.get_listing(lid)
    assert row["price"] == 40.0
    assert row["status"] == "new"  # requeued for re-scoring
    history = store.conn.execute(
        "SELECT price FROM price_history WHERE listing_id=? ORDER BY rowid",
        (lid,)).fetchall()
    assert [h["price"] for h in history] == [50.0, 40.0]


def test_price_cache_roundtrip_and_expiry(store):
    store.cache_price("75192-1", "used", "bricklink", 520.0, 14)
    hit = store.cached_price("75192-1", "used", "bricklink", max_age_days=7)
    assert hit["avg_price"] == 520.0
    assert store.cached_price("75192-1", "used", "bricklink", max_age_days=0) is None
    assert store.cached_price("99999-1", "used", "bricklink", max_age_days=7) is None


def test_alert_dedupe(store):
    lid, _, _ = store.upsert_listing(make_listing())
    assert not store.was_alerted(lid, "ntfy")
    store.mark_alerted(lid, "ntfy")
    assert store.was_alerted(lid, "ntfy")


def test_email_dedupe(store):
    assert not store.email_processed("<abc@mail>")
    store.record_email("<abc@mail>", "fb", "new listing", "<html/>", 2, True)
    assert store.email_processed("<abc@mail>")
