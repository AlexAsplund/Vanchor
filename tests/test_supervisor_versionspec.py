"""Tests for vanchor_supervisor.versionspec — PEP-440 alpha-aware versioning."""
from __future__ import annotations
import pytest
from vanchor_supervisor.versionspec import parse_version, is_at_least


# ------------------------------------------------------------------ #
# parse_version
# ------------------------------------------------------------------ #

def test_parse_release():
    assert parse_version("1.5.0") == (1, 5, 0, 3, 0)


def test_parse_alpha():
    assert parse_version("1.5.0a8") == (1, 5, 0, 0, 8)


def test_parse_beta():
    assert parse_version("1.5.0b2") == (1, 5, 0, 1, 2)


def test_parse_rc():
    assert parse_version("2.0.0rc1") == (2, 0, 0, 2, 1)


def test_parse_zero():
    assert parse_version("0.1.0") == (0, 1, 0, 3, 0)


def test_ordering_pre_lt_release():
    # a < b < rc < final
    assert parse_version("1.5.0a8") < parse_version("1.5.0b1")
    assert parse_version("1.5.0b1") < parse_version("1.5.0rc1")
    assert parse_version("1.5.0rc1") < parse_version("1.5.0")


def test_ordering_alpha_numbers():
    assert parse_version("1.5.0a1") < parse_version("1.5.0a8")
    assert parse_version("1.5.0a8") < parse_version("1.5.0a9")


def test_ordering_major():
    assert parse_version("1.0.0") < parse_version("2.0.0")


def test_ordering_minor():
    assert parse_version("1.4.9") < parse_version("1.5.0")


def test_parse_error_garbage():
    with pytest.raises(ValueError):
        parse_version("garbage")


def test_parse_error_partial():
    with pytest.raises(ValueError):
        parse_version("1.5")


def test_parse_error_empty():
    with pytest.raises(ValueError):
        parse_version("")


def test_parse_error_dev():
    with pytest.raises(ValueError):
        parse_version("1.5.0.dev1")


# ------------------------------------------------------------------ #
# is_at_least
# ------------------------------------------------------------------ #

def test_is_at_least_equal():
    assert is_at_least("0.1.0", "0.1.0")


def test_is_at_least_newer_installed():
    assert is_at_least("0.2.0", "0.1.0")


def test_is_at_least_older_installed():
    assert not is_at_least("0.1.0", "0.2.0")


def test_is_at_least_alpha_not_meeting_release():
    assert not is_at_least("1.5.0a8", "1.5.0")


def test_is_at_least_release_meets_alpha():
    assert is_at_least("1.5.0", "1.5.0a8")


def test_is_at_least_same_alpha():
    assert is_at_least("1.5.0a8", "1.5.0a8")


def test_is_at_least_higher_alpha():
    assert is_at_least("1.5.0a9", "1.5.0a8")


def test_is_at_least_lower_alpha():
    assert not is_at_least("1.5.0a7", "1.5.0a8")


def test_is_at_least_future_major():
    assert is_at_least("2.0.0", "1.9.9")


def test_is_at_least_supervisor_zero():
    # The supervisor starts at 0.1.0; app bundles require min_supervisor=0.1.0
    assert is_at_least("0.1.0", "0.1.0")
    assert not is_at_least("0.0.9", "0.1.0")
