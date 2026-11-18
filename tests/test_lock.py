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


def test_pin_guard_lockout_doubles_after_repeated_failures():
    guard = lock.PinGuard()
    for _ in range(3):
        guard.fail(100)
    assert guard.check(100) == 60
    for _ in range(3):
        guard.fail(200)
    assert guard.check(200) == 120
    guard.ok()
    for _ in range(3):
        guard.fail(1000)
    assert guard.check(1000) == 60


def test_pin_guard_lockout_is_capped_at_one_hour():
    guard = lock.PinGuard()
    for _ in range(30):
        guard.fail(0)
    assert guard.check(0) == 3600


def test_clock_uses_given_timezone_not_process_timezone():
    from datetime import timezone, timedelta

    zone = timezone(timedelta(hours=1))
    window_end = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc).timestamp()
    r = lock.evaluate(inputs(window_ends=window_end, tz=zone))
    assert r.until == "19:00"


def test_grant_after_limit_exhausted_adds_bonus_seconds():
    assert lock.bonus_for_grant(ts(14), ts(14, 30), ts(14, 10), True) == 20 * 60
    assert lock.bonus_for_grant(ts(15), ts(15, 30), ts(14, 10), True) == 30 * 60


def test_grant_before_limit_exhausted_adds_no_bonus():
    assert lock.bonus_for_grant(ts(14), ts(14, 30), ts(14, 10), False) == 0


def test_shortened_or_cleared_grant_adds_no_bonus():
    assert lock.bonus_for_grant(ts(15), ts(14, 30), ts(14, 10), True) == 0


def test_manual_lock_locks_outside_window_and_beats_every_grant():
    r = lock.evaluate(inputs(window_on=False, manual_lock=True))
    assert r.locked and "rodziców" in r.reason
    r = lock.evaluate(
        inputs(manual_lock=True, allowed=True, parent_until=ts(19), kid_until=ts(16))
    )
    assert r.locked and "rodziców" in r.reason


def test_manual_lock_off_changes_nothing():
    assert not lock.evaluate(inputs(window_on=False, manual_lock=False)).locked
    assert lock.evaluate(inputs(manual_lock=False)).locked


def test_grant_minutes_counts_from_the_later_of_old_grant_and_now():
    assert lock.grant_minutes(ts(14), ts(14, 30), ts(14, 10)) == 20
    assert lock.grant_minutes(ts(15), ts(15, 30), ts(14, 10)) == 30
    assert lock.grant_minutes(ts(15), ts(14, 30), ts(14, 10)) == 0


def test_grant_text_is_polish_and_names_the_end_time():
    assert lock.grant_text(30, ts(14, 40)) == "Dostałeś +30 min. Oglądaj do 14:40."
    assert lock.grant_text(60, ts(15, 10)) == "Dostałeś +1 h. Oglądaj do 15:10."
    assert lock.grant_text(90, ts(15, 40)) == "Dostałeś +90 min. Oglądaj do 15:40."


def test_request_notification_matches_the_children_flow():
    p = lock.request_payload("Czas bez telewizora do 19:00")
    assert p["title"] == "Salon: prośba o czas"
    assert "Czas bez telewizora do 19:00" in p["message"]
    actions = p["data"]["actions"]
    assert [a["action"] for a in actions] == ["LUMA_GRANT_15", "LUMA_GRANT_30", "LUMA_DENY"]
    assert [a["title"] for a in actions] == ["+15 min", "+30 min", "Odrzuć i wyłącz TV"]
    assert actions[2]["destructive"] is True
    assert p["data"]["url"] == "/kontrola-rodzicielska/salon"
    assert p["data"]["clickAction"] == "/kontrola-rodzicielska/salon"
    assert p["data"]["tag"] == "luma_prosba_salon"


def test_time_left_is_the_nearest_of_grant_daily_and_app_limits():
    base = dict(kid_until=ts(15), now=ts(14, 30))
    assert lock.time_left(inputs(**base)) == 30 * 60
    assert lock.time_left(inputs(**base, daily_limit_s=3600, daily_used_s=3000)) == 600
    assert (
        lock.time_left(
            inputs(
                **base,
                current_app="netflix",
                app_limits_s={"netflix": 1200},
                app_used_s={"netflix": 900},
            )
        )
        == 300
    )


def test_time_left_is_none_without_a_child_grant_or_when_locked():
    assert lock.time_left(inputs(now=ts(14))) is None
    assert lock.time_left(inputs(window_on=False)) is None
    assert lock.time_left(inputs(kid_until=ts(15), manual_lock=True)) is None
    assert lock.time_left(inputs(parent_until=ts(19), kid_until=ts(15))) is None
    assert (
        lock.time_left(inputs(kid_until=ts(15), daily_limit_s=60, daily_used_s=60)) is None
    )


def test_warning_text_names_the_minutes_left():
    assert lock.warning_text(900) == (
        "Za 15 min czas się skończy i telewizor zostanie zablokowany."
    )
    assert lock.warning_text(50).startswith("Za 1 min ")
