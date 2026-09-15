"""Tests for _parse_interval_days, _needs_sync, and _is_blacklisted helpers in main."""

import pytest

from main import _parse_interval_days, _needs_sync, _is_blacklisted


class TestParseIntervalDays:
    def test_valid_integer(self):
        assert _parse_interval_days({"full_check_interval_days": 14}) == 14

    def test_valid_string_integer(self):
        # JSON loaded from an env var may arrive as a string.
        assert _parse_interval_days({"full_check_interval_days": "7"}) == 7

    def test_missing_key_defaults_to_28(self):
        assert _parse_interval_days({}) == 28

    def test_none_value_defaults_to_28(self):
        assert _parse_interval_days({"full_check_interval_days": None}) == 28

    def test_empty_string_defaults_to_28(self):
        assert _parse_interval_days({"full_check_interval_days": ""}) == 28

    def test_non_numeric_string_defaults_to_28(self):
        assert _parse_interval_days({"full_check_interval_days": "fortnight"}) == 28

    def test_float_is_truncated(self):
        assert _parse_interval_days({"full_check_interval_days": 3.9}) == 3

    def test_negative_clamped_to_zero(self):
        assert _parse_interval_days({"full_check_interval_days": -5}) == 0

    def test_zero_allowed(self):
        assert _parse_interval_days({"full_check_interval_days": 0}) == 0

    def test_large_value_preserved(self):
        assert _parse_interval_days({"full_check_interval_days": 365}) == 365


# ---------------------------------------------------------------------------
# _needs_sync
# ---------------------------------------------------------------------------

class TestNeedsSync:
    # Shared constants — 28-day interval in seconds, a "now" timestamp large
    # enough that offsets up to 30 days in the past remain positive.
    INTERVAL = 28 * 86400
    NOW = 100 * 86400.0  # ~day 100 of the epoch

    # Shorthand: call with defaults that represent an already-synced comic
    def _call(self, **kwargs):
        defaults = dict(
            local_count=10,
            remote_count=10,
            in_updated_feed=False,
            last_synced=self.NOW - 3600,   # synced 1 hour ago
            full_check_interval_s=self.INTERVAL,
            now=self.NOW,
        )
        defaults.update(kwargs)
        return _needs_sync(**defaults)

    def test_never_downloaded_always_syncs(self):
        assert self._call(local_count=0) is True

    def test_count_changed_syncs(self):
        assert self._call(local_count=8, remote_count=10) is True

    def test_count_unchanged_not_in_feed_no_sync(self):
        assert self._call(in_updated_feed=False) is False

    def test_in_feed_recently_synced_no_sync(self):
        # last_synced 1 hour ago, interval 28 days — should skip
        assert self._call(in_updated_feed=True, last_synced=self.NOW - 3600) is False

    def test_in_feed_never_synced_does_sync(self):
        # last_synced=0 means never synced — should sync even if count is same
        assert self._call(in_updated_feed=True, last_synced=0) is True

    def test_in_feed_stale_synced_does_sync(self):
        # last_synced 30 days ago, interval 28 days — should re-sync
        stale = self.NOW - (30 * 86400)
        assert self._call(in_updated_feed=True, last_synced=stale) is True

    def test_in_feed_synced_just_within_interval_no_sync(self):
        # last_synced 27 days ago, interval 28 days — still within interval
        recent = self.NOW - (27 * 86400)
        assert self._call(in_updated_feed=True, last_synced=recent) is False

    def test_zero_interval_always_syncs_when_in_feed(self):
        # interval=0 means anything is "stale", so in-feed comics always re-sync
        assert self._call(in_updated_feed=True, last_synced=self.NOW - 1, full_check_interval_s=0) is True


# ---------------------------------------------------------------------------
# _is_blacklisted
# ---------------------------------------------------------------------------

class TestIsBlacklisted:
    def test_matching_tag(self):
        assert _is_blacklisted(["Anal", "Gay", "Oral"], ["Gay"]) is True

    def test_no_match(self):
        assert _is_blacklisted(["Anal", "Oral"], ["Gay"]) is False

    def test_empty_tags(self):
        assert _is_blacklisted([], ["Gay"]) is False

    def test_empty_blacklist(self):
        assert _is_blacklisted(["Anal", "Gay"], []) is False

    def test_case_insensitive_lower_tag(self):
        assert _is_blacklisted(["gay"], ["Gay"]) is True

    def test_case_insensitive_upper_blacklist(self):
        assert _is_blacklisted(["Gay"], ["GAY"]) is True

    def test_partial_match_not_triggered(self):
        # "Gay" alone should not match a blacklist entry of "Gay Porn"
        assert _is_blacklisted(["Gay"], ["Gay Porn"]) is False

    def test_multiple_blacklist_entries_one_matches(self):
        assert _is_blacklisted(["Oral", "Mini Girl"], ["AI Generated", "Mini Girl"]) is True
