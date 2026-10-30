
from app.services.research_service import _feed_matches_topic


def test_israel_election_headline_matches():
    assert _feed_matches_topic(
        "Israel's election too close to call",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_unrelated_us_house_election_does_not_match():
    assert not _feed_matches_topic(
        "These are the House races to watch in the 2026 midterm election",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_unrelated_headline_does_not_match():
    assert not _feed_matches_topic(
        "GDP report shows inflation is slowing",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_netanyahu_election_headline_matches():
    assert _feed_matches_topic(
        "Netanyahu's bloc leading Israel election but short of majority",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_house_races_headline_is_excluded():
    assert not _feed_matches_topic(
        "These are the House races to watch in the 2026 midterm election",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_gdp_headline_is_excluded():
    assert not _feed_matches_topic(
        "GDP report shows inflation is slowing",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )

    
def test_israel_deadlock_article_matches():
    assert _feed_matches_topic(
        "How could Israel break election deadlock? Here are four scenarios",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_netanyahu_election_article_matches():
    assert _feed_matches_topic(
        "Netanyahu's bloc leading Israel election but short of majority",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_us_house_races_article_is_rejected():
    assert not _feed_matches_topic(
        "These are the House races to watch in the 2026 midterm election",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )


def test_unrelated_gdp_article_is_rejected():
    assert not _feed_matches_topic(
        "GDP report shows inflation is slowing",
        "Israel's election deadlock is a sign of the country's deep divisions",
    )

