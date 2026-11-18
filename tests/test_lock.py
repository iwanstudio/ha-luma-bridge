from datetime import datetime
import math

import pytest

import lock


def ts(h: int, m: int = 0, day: int = 18) -> float:
    return datetime(2026, 11, day, h, m).timestamp()


def inputs(**kw) -> lock.LockInputs:
    base = dict(
        window_on=True,
        now=ts(14),
        window_ends=ts(19),
        allowed=False,
        kid_until=0.0,
        parent_until=0.0,
        daily_limit_s=0,
        daily_used_s=0,
        app_limits_s={},
        app_used_s={},
        current_app=None,
        tv_on=True,
    )
    base.update(kw)
    return lock.LockInputs(**base)


def test_pin_leading_zero_not_equal_to_stripped():
    salt = lock.new_salt()
    digest = lock.hash_pin("0042", salt)
    assert lock.verify_pin("0042", salt, digest)
    assert not lock.verify_pin("42", salt, digest)


@pytest.mark.parametrize("pin", ["", "12a4", "123456789", " 1234", "１２３４"])
def test_pin_rejects_non_digits_and_too_long(pin):
    salt = lock.new_salt()
    digest = lock.hash_pin("1234", salt)
    assert not lock.verify_pin(pin, salt, digest)


def test_pin_guard_locks_after_three_failures():
    guard = lock.PinGuard()
    for _ in range(3):
        guard.fail(100)
    assert guard.check(101) == 59
    assert guard.check(161) == 0
    guard.ok()
    assert guard.check(101) == 0


def test_outside_window_never_locked():
    r = lock.evaluate(inputs(window_on=False))
    assert not r.locked and r.blocked_apps == [] and not r.kid_mode


def test_window_without_grant_locked_with_reason_and_until():
    r = lock.evaluate(inputs())
    assert r.locked
    assert "Czas bez telewizora" in r.reason
    assert r.until == "19:00"


def test_parent_unlock_ends_with_window_and_ignores_limits():
    r = lock.evaluate(
        inputs(parent_until=ts(19), daily_limit_s=60, daily_used_s=999)
    )
    assert not r.locked and not r.kid_mode and r.blocked_apps == []


def test_allowed_switch_behaves_like_parent_mode():
    r = lock.evaluate(inputs(allowed=True, daily_limit_s=60, daily_used_s=999))
    assert not r.locked and not r.kid_mode


def test_kid_grant_unlocks_until_daily_limit():
    r = lock.evaluate(inputs(kid_until=ts(15), daily_limit_s=5400, daily_used_s=100))
    assert not r.locked and r.kid_mode
    r = lock.evaluate(inputs(kid_until=ts(15), daily_limit_s=5400, daily_used_s=5400))
    assert r.locked and "Limit dzienny wyczerpany" in r.reason


def test_app_limit_blocks_only_that_app():
    r = lock.evaluate(
        inputs(
            kid_until=ts(15),
            app_limits_s={"netflix": 600, "youtube": 600},
            app_used_s={"netflix": 600, "youtube": 10},
        )
    )
    assert not r.locked
    assert r.blocked_apps == ["Netflix"]


def test_zero_limit_means_unlimited():
    r = lock.evaluate(
        inputs(
            kid_until=ts(15),
            daily_limit_s=0,
            daily_used_s=10**6,
            app_limits_s={"netflix": 0},
            app_used_s={"netflix": 10**6},
        )
    )
    assert not r.locked and r.blocked_apps == []


def test_grant_expiring_exactly_now_is_locked():
    r = lock.evaluate(inputs(now=ts(15), kid_until=ts(15)))
    assert r.locked


def test_unknown_app_not_counted_or_blocked():
    counter = lock.UsageCounter("2026-11-18")
    counter.tick("2026-11-18", 10, None, True)
    assert counter.total == 10 and counter.apps == {}
    assert lock.app_key("HDMI 1", {}) is None
    assert lock.app_key(None, {}) is None


def test_app_key_matches_known_and_extra_names_case_insensitively():
    assert lock.app_key("Netflix", {}) == "netflix"
    assert lock.app_key("YOUTUBE", {}) == "youtube"
    assert lock.app_key("Moja Apka", {"moja apka": "Moja Apka"}) == "Moja Apka"


def test_counter_resets_on_new_day_and_not_when_tv_off():
    counter = lock.UsageCounter("2026-11-18")
    counter.tick("2026-11-18", 10, "netflix", True)
    counter.tick("2026-11-18", 10, "netflix", False)
    assert counter.total == 10 and counter.apps == {"netflix": 10}
    counter.tick("2026-11-19", 10, "netflix", True)
    assert counter.total == 10 and counter.day == "2026-11-19"


def test_counter_roundtrip_survives_restart():
    counter = lock.UsageCounter("2026-11-18")
    counter.tick("2026-11-18", 30, "youtube", True)
    again = lock.UsageCounter.from_dict(counter.to_dict())
    assert again.total == 30 and again.apps == {"youtube": 30}
    assert math.isclose(again.total, counter.total)
