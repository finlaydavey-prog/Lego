from conftest import FIXTURES

from legomon.sources.gumtree import GumtreeSource


def make_source(config, store):
    return GumtreeSource(config, store)


def test_client_data_parse(config, store):
    html = (FIXTURES / "gumtree_sample.html").read_text()
    listings = make_source(config, store).parse_client_data(html)
    assert len(listings) == 3
    l = {x.source_id: x for x in listings}
    # nearby ads (just outside the radius) are captured and tagged
    assert l["1500000001"].attributes.get("nearby") == "true"
    assert "nearby" not in l["1513537216"].attributes
    goblin = l["1513537216"]
    assert goblin.price == 15.0
    assert "Green Goblin" in goblin.title
    assert goblin.url.startswith("https://www.gumtree.com/p/")
    assert goblin.posted_at is not None
    assert goblin.attributes.get("Condition") == "New"
    bundle = l["1514069185"]
    assert len(bundle.image_urls) == 2
    assert "75192" in bundle.description


def test_css_fallback_parse(config, store):
    html = (FIXTURES / "gumtree_sample.html").read_text()
    # simulate clientData disappearing
    html = html.replace("window.clientData", "window.renamedData")
    src = make_source(config, store)
    assert src.parse_client_data(html) == []
    listings = src.parse(html)
    assert len(listings) == 1
    assert listings[0].source_id == "1513537216"
    assert listings[0].price == 15.0


def test_search_urls_include_params(config, store):
    urls = make_source(config, store).search_urls()
    assert any("q=lego+job+lot" in u for u in urls)
    assert all("distance=2" in u for u in urls)
    assert all("NE6+5SD" in u for u in urls)
    # category sweep included
    assert any("search_category=baby-kids-stuff" in u for u in urls)
