from __future__ import annotations

from api_key_pool import parse_api_key_pool


def test_parse_api_key_pool_dedupes_and_preserves_order() -> None:
    keys = parse_api_key_pool(
        "alpha,beta",
        "beta;gamma",
        "gamma alpha",
    )
    assert keys == ("alpha", "beta", "gamma")


def test_parse_api_key_pool_ignores_empty_fragments() -> None:
    assert parse_api_key_pool("", "  ", ",;;") == ()
