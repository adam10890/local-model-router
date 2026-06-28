"""Tests for context utilization tiers (Agent Zero context_analyzer parity)."""

from local_model_router.helpers.context_calculator import (
    ContextUtilization,
    EFFECTIVE_CTX_RATIO,
    utilization_zone,
)


class TestUtilizationZone:
    def test_unknown_when_total_non_positive(self):
        assert utilization_zone(100, 0) == "unknown"
        assert utilization_zone(100, -5) == "unknown"

    def test_zone_boundaries(self):
        # green up to and including 50%
        assert utilization_zone(0, 1000) == "green"
        assert utilization_zone(500, 1000) == "green"
        # yellow up to and including 70%
        assert utilization_zone(501, 1000) == "yellow"
        assert utilization_zone(700, 1000) == "yellow"
        # orange up to and including 85%
        assert utilization_zone(701, 1000) == "orange"
        assert utilization_zone(850, 1000) == "orange"
        # red above 85%
        assert utilization_zone(851, 1000) == "red"
        assert utilization_zone(1000, 1000) == "red"


class TestContextUtilization:
    def test_percent_and_remaining(self):
        u = ContextUtilization(used=2048, total=8192)
        assert u.percent == 25.0
        assert u.remaining == 8192 - 2048
        assert u.zone == "green"

    def test_percent_zero_when_no_window(self):
        u = ContextUtilization(used=100, total=0)
        assert u.percent == 0.0
        assert u.zone == "unknown"
        assert u.remaining == 0

    def test_effective_budget_matches_ratio(self):
        u = ContextUtilization(used=0, total=10000)
        assert u.effective_budget == int(10000 * EFFECTIVE_CTX_RATIO)

    def test_over_budget_flag(self):
        total = 8192
        budget = int(total * EFFECTIVE_CTX_RATIO)
        assert ContextUtilization(used=budget, total=total).over_budget is False
        assert ContextUtilization(used=budget + 1, total=total).over_budget is True

    def test_over_budget_false_without_window(self):
        assert ContextUtilization(used=99999, total=0).over_budget is False
