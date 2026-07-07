from legomon.util import (domain_allowed, facebook_item_id, parse_price,
                          parse_relative_date, sanitize_text,
                          unwrap_facebook_url)


def test_unwrap_facebook_redirect():
    wrapped = ("https://l.facebook.com/l.php?u=https%3A%2F%2Fwww.facebook.com"
               "%2Fmarketplace%2Fitem%2F1234567890123456%2F%3Fref%3Demail&h=AT0xyz")
    assert unwrap_facebook_url(wrapped) == \
        "https://www.facebook.com/marketplace/item/1234567890123456/"


def test_unwrap_passthrough_non_fb():
    url = "https://www.gumtree.com/p/foo/123"
    assert unwrap_facebook_url(url) == url


def test_unwrap_strips_fb_tracking_params():
    url = "https://www.facebook.com/marketplace/category/toys?tracking=abc"
    assert "?" not in unwrap_facebook_url(url)


def test_facebook_item_id():
    assert facebook_item_id(
        "https://www.facebook.com/marketplace/item/987654/") == "987654"
    assert facebook_item_id("https://www.gumtree.com/p/x/1") is None


def test_domain_allowed():
    allowed = ["facebook.com", "gumtree.com", "ebay.co.uk"]
    assert domain_allowed("https://www.facebook.com/marketplace/item/1/", allowed)
    assert domain_allowed("https://l.facebook.com/l.php?u=x", allowed)
    assert not domain_allowed("https://evil.example.com/facebook.com/", allowed)
    assert not domain_allowed("https://notfacebook.com/item", allowed)


def test_parse_price():
    assert parse_price("£15.00") == 15.0
    assert parse_price("£1,250") == 1250.0
    assert parse_price("15") == 15.0
    assert parse_price(None) is None
    assert parse_price("free to collector") is None


def test_sanitize_strips_control_chars():
    assert sanitize_text("hi\x00\x1bthere\nok") == "hi there\nok".replace(" ", "")  \
        or sanitize_text("hi\x00\x1bthere\nok") == "hithere\nok"


def test_sanitize_caps_length():
    assert len(sanitize_text("a" * 9000, max_len=100)) == 100


def test_parse_relative_date():
    d = parse_relative_date("57 days ago")
    assert d is not None
    assert parse_relative_date("just now") is not None
    assert parse_relative_date("banana") is None
