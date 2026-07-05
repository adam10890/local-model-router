from local_model_router.helpers import stats_tracker


def _isolated_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(stats_tracker, "_stats", None)
    monkeypatch.setattr(stats_tracker, "_stats_path", tmp_path / "stats.json")


def test_stats_tracker_keeps_only_failover_telemetry(monkeypatch, tmp_path):
    _isolated_stats(monkeypatch, tmp_path)

    stats_tracker.record_failover("chat", "utility", "chat unhealthy")
    summary = stats_tracker.get_stats_summary("24h")

    assert summary == {
        "window": "24h",
        "failovers": {
            "total": 1,
            "by_reason": {"chat unhealthy": 1},
            "by_slot": {"chat": 1},
            "last_at": summary["failovers"]["last_at"],
        },
        "slots": [],
    }
    assert not hasattr(stats_tracker, "record_request")


def test_stats_tracker_loads_legacy_failover_shape(monkeypatch, tmp_path):
    _isolated_stats(monkeypatch, tmp_path)
    (tmp_path / "stats.json").write_text(
        '{"failovers":{"total_failovers":2,"by_reason":{"x":2},'
        '"by_slot":{"chat":2},"last_failover_at":"2026-01-01T00:00:00Z"}}',
        encoding="utf-8",
    )

    assert stats_tracker.get_stats_summary()["failovers"] == {
        "total": 2,
        "by_reason": {"x": 2},
        "by_slot": {"chat": 2},
        "last_at": "2026-01-01T00:00:00Z",
    }
