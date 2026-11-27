"""Unit tests for operator configuration helpers."""

from __future__ import annotations

from common import _parse_restic_resources, spread_delay_seconds


def test_parse_restic_resources_coerces_numbers_to_strings() -> None:
    # Helm renders unquoted YAML numbers (cpu: 1) as JSON numbers.
    assert _parse_restic_resources(
        '{"requests": {"cpu": 1, "memory": "128Mi"}, "limits": {"cpu": 0.5}}'
    ) == {
        "requests": {"cpu": "1", "memory": "128Mi"},
        "limits": {"cpu": "0.5"},
    }


def test_parse_restic_resources_ignores_non_objects() -> None:
    assert _parse_restic_resources("null") == {}
    assert _parse_restic_resources('{"requests": "nope"}') == {}


def test_spread_delay_is_stable_and_bounded() -> None:
    assert spread_delay_seconds("sched", "data", 0) == 0
    assert spread_delay_seconds("sched", "data", -1) == 0

    delay = spread_delay_seconds("sched", "data", 300)
    assert 0 <= delay < 300
    assert spread_delay_seconds("sched", "data", 300) == delay
    assert spread_delay_seconds("sched", "logs", 300) != delay
