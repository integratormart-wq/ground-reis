"""Offline characterization, NOT a live reproduction of deal -> SPA transfer.

The current revision has no enrich_trip_from_deal implementation. These tests
protect the existing SPA <-> app datetime boundary against an unjustified +2h
correction. server.time responses are synthetic fixtures, not portal evidence.
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from backend import bitrix


@pytest.fixture(autouse=True)
def isolated_timezone_cache():
    bitrix._clear_runtime_cache()
    yield
    bitrix._clear_runtime_cache()


def portal_at(monkeypatch, timestamp):
    def post(url, method, params):
        assert method == "server.time"
        return {"result": timestamp}
    monkeypatch.setattr(bitrix, "_http_post", post)


@pytest.mark.parametrize("source, expected_day, expected_time", [
    ("2026-09-11T19:00:00+03:00", date(2026, 9, 11), "19:00"),
    ("2026-09-11T16:00:00Z", date(2026, 9, 11), "19:00"),
    # Same instant, different wall time: this alone does NOT prove a bug.
    ("2026-09-11T19:00:00+05:00", date(2026, 9, 11), "17:00"),
    ("2026-09-11T23:30:00Z", date(2026, 9, 12), "02:30"),
])
def test_inbound_preserves_instant_in_server_offset(monkeypatch, source, expected_day, expected_time):
    portal_at(monkeypatch, "2026-09-11T12:00:00+03:00")
    assert bitrix._parse_bitrix_planned_at("https://example.invalid/", source) == (
        expected_day, expected_time,
    )


@pytest.mark.parametrize("source", ["2026-09-11T19:00:00", "11.09.2026 19:00"])
def test_naive_input_is_not_shifted(monkeypatch, source):
    def no_network(*args):
        pytest.fail("A timezone-less wall time must not query a timezone")
    monkeypatch.setattr(bitrix, "_http_post", no_network)
    assert bitrix._parse_bitrix_planned_at("https://example.invalid/", source) == (
        date(2026, 9, 11), "19:00",
    )


def test_unavailable_timezone_does_not_invent_two_hour_correction(monkeypatch):
    monkeypatch.setattr(bitrix, "_http_post", lambda *args: {"error": "unavailable"})
    assert bitrix._parse_bitrix_planned_at(
        "https://example.invalid/", "2026-09-11T19:00:00+05:00",
    ) == (date(2026, 9, 11), "19:00")


def test_inbound_outbound_roundtrip_preserves_instant(monkeypatch):
    portal_at(monkeypatch, "2026-09-11T12:00:00+03:00")
    source = "2026-09-11T19:00:00+05:00"
    day, clock = bitrix._parse_bitrix_planned_at("https://example.invalid/", source)
    request = SimpleNamespace(planned_date=day, planned_time=clock)
    outbound = bitrix._planned_at_outbound_value(request, "https://example.invalid/")
    assert outbound == "2026-09-11T17:00+03:00"
    assert datetime.fromisoformat(outbound) == datetime.fromisoformat(source)
