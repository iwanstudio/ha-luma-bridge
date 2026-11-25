import stats


def watch(history, day, app, ticks):
    for _ in range(ticks):
        history.tick(day, 10, app)


def test_counts_time_and_launches_per_app():
    h = stats.History()
    watch(h, "2026-11-25", "Netflix", 6)
    watch(h, "2026-11-25", "YouTube", 3)
    watch(h, "2026-11-25", "Netflix", 6)
    assert h.ranking("2026-11-25", 1) == [
        {"name": "Netflix", "minutes": 2.0, "launches": 2},
        {"name": "YouTube", "minutes": 0.5, "launches": 1},
    ]
    assert h.total_minutes("2026-11-25") == 2.5


def test_a_pause_makes_the_next_use_a_new_launch():
    h = stats.History()
    watch(h, "2026-11-25", "Netflix", 1)
    h.tick("2026-11-25", 10, None)
    watch(h, "2026-11-25", "Netflix", 1)
    assert h.ranking("2026-11-25", 1)[0]["launches"] == 2


def test_ranking_covers_only_the_asked_days():
    h = stats.History()
    watch(h, "2026-11-17", "YouTube", 60)
    watch(h, "2026-11-19", "Netflix", 6)
    watch(h, "2026-11-25", "Netflix", 6)
    assert [row["name"] for row in h.ranking("2026-11-25", 1)] == ["Netflix"]
    assert [row["name"] for row in h.ranking("2026-11-25", 7)] == ["Netflix"]
    assert h.ranking("2026-11-25", 7)[0]["minutes"] == 2.0
    assert h.ranking("2026-11-25", 30)[0]["name"] == "YouTube"


def test_old_days_are_dropped_and_data_survives_a_round_trip():
    h = stats.History()
    watch(h, "2026-09-01", "Netflix", 6)
    watch(h, "2026-11-25", "YouTube", 6)
    copy = stats.History.from_dict(h.to_dict())
    assert list(copy.days) == ["2026-11-25"]
    assert copy.total_minutes("2026-11-25") == 1.0


def test_screens_that_are_not_viewing_are_ignored():
    assert stats.viewed_app(True, "netflix", "Netflix") == "Netflix"
    assert stats.viewed_app(True, "com.ecklf.luma", "Luma") is None
    assert stats.viewed_app(True, "com.webos.app.home", None) is None
    assert stats.viewed_app(False, "netflix", "Netflix") is None
    assert stats.viewed_app(True, "x", "") is None
