from app.services import credibility


def test_known_wire_domain_outranks_unlisted_domain():
    assert credibility.tier_for("reuters.com") == 1
    assert credibility.tier_for("some-random-blog.example") == credibility.DEFAULT_TIER
    assert credibility.score_for("reuters.com") > credibility.score_for("some-random-blog.example")


def test_www_prefix_and_case_are_normalized():
    assert credibility.tier_for("WWW.Reuters.com") == credibility.tier_for("reuters.com")
