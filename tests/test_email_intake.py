from conftest import FIXTURES

from legomon.sources.email_intake import EmailIntakeSource


def make_source(config, store):
    return EmailIntakeSource(config, store)


FB_RULE = {"name": "facebook-marketplace",
           "from_domains": ["facebookmail.com"],
           "subject_patterns": ["new listing", "matches your search"],
           "source": "facebook"}


def test_extracts_and_unwraps_fb_listings(config, store):
    html = (FIXTURES / "fb_alert_email.html").read_text()
    listings = make_source(config, store).extract_listings(html, FB_RULE)
    ids = {l.source_id for l in listings}
    assert ids == {"1234567890123456", "9876543210987654"}
    by_id = {l.source_id: l for l in listings}
    falcon = by_id["1234567890123456"]
    assert falcon.url == "https://www.facebook.com/marketplace/item/1234567890123456/"
    assert falcon.source == "facebook"
    assert falcon.price == 45.0
    assert "75257" in falcon.title
    assert any("thumb1" in u for u in falcon.image_urls)


def test_disallowed_domain_dropped(config, store):
    html = (FIXTURES / "fb_alert_email.html").read_text()
    listings = make_source(config, store).extract_listings(html, FB_RULE)
    assert all("evil.example.com" not in l.url for l in listings)
    assert "5555555555" not in {l.source_id for l in listings}


def test_rule_matching(config, store):
    src = make_source(config, store)
    assert src.match_rule("Facebook <noreply@facebookmail.com>",
                          "New listings that match your search") is not None
    assert src.match_rule("Facebook <noreply@facebookmail.com>",
                          "Your weekly digest") is None
    assert src.match_rule("random@spam.com", "new listing for you!") is None
    # gumtree rule from config.yaml
    assert src.match_rule("alerts@mail.gumtree.com",
                          "New ad matching 'lego'") is not None


def test_dedupe_against_store(config, store):
    """End-to-end: same item id must not be ingested twice."""
    from legomon.pipeline import Pipeline
    html = (FIXTURES / "fb_alert_email.html").read_text()
    listings = make_source(config, store).extract_listings(html, FB_RULE)
    pipe = Pipeline(config, store)
    config.data["storage"]["download_photos"] = False
    first = pipe.ingest(listings)
    second = pipe.ingest(listings)
    assert len(first) == 2
    assert second == []
