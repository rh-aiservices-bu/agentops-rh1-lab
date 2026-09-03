"""Tests for maintenance history and work orders.

The golden workflow and Scenario 2 both depend on specific records being
retrievable by an ordinary query, so those are pinned explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from plant_api import maintenance


@pytest.fixture
def store():
    return maintenance.seed()


# --------------------------------------------------------------------------
# The golden workflow's diagnosis step
# --------------------------------------------------------------------------


def test_pump_4_history_surfaces_the_prior_bearing_failure(store):
    """Step 2 of the golden workflow: `search_maintenance_history(pump=4)`.

    The agent has to be able to connect today's vibration to the bearing
    replacement fourteen months ago, so that record must come back from a plain
    per-pump query with no clever search terms.
    """
    hits = store.search(pump_id=4)
    ids = [r.id for r in hits]

    assert "MR-2246" in ids
    prior = store.record("MR-2246")
    assert prior.type == "replacement"
    assert "bearing" in prior.summary.lower()
    # The detail that makes the current fault explicable.
    assert "NOT replaced" in prior.notes


def test_pump_4_history_is_newest_first(store):
    hits = store.search(pump_id=4)
    dates = [r.date for r in hits]
    assert dates == sorted(dates, reverse=True)


def test_free_text_search_finds_bearing_work_across_pumps(store):
    hits = store.search(query="bearing")
    assert {"MR-2246", "MR-2291"}.issubset({r.id for r in hits})


# --------------------------------------------------------------------------
# Scenario 2 — the poisoned record
# --------------------------------------------------------------------------


def test_poisoned_record_is_returned_by_an_innocent_query(store):
    """The whole point of indirect injection: no malicious prompt required.

    An attendee asking a completely reasonable question about Pump 4 must
    retrieve MR-2291 without trying to.
    """
    hits = store.search(pump_id=4)
    assert "MR-2291" in [r.id for r in hits]


def test_poisoned_record_carries_the_injection(store):
    record = store.record("MR-2291")

    assert "diagnostics.example.com" in record.notes
    assert "IMPORTANT DIAGNOSTIC PROCEDURE" in record.notes
    # Plausible provenance is what makes it a teaching artifact rather than a
    # cartoon: a contractor's tooling appended it to a real record.
    assert "NorthTech" in record.technician
    assert record.pump_id == 4


def test_poisoned_record_is_otherwise_a_legitimate_record(store):
    """If it reads as obviously fake, attendees learn the wrong lesson."""
    record = store.record("MR-2291")

    assert record.type == "survey"
    assert record.labour_hours > 0
    assert "vibration" in record.summary.lower()


# --------------------------------------------------------------------------
# Work orders
# --------------------------------------------------------------------------


def test_first_created_work_order_is_wo_4417(store):
    """The lab guide names WO-4417 in the golden workflow trace."""
    wo = store.create_work_order(
        pump_id=4,
        title="Bearing inspection — Pump 4",
        description="Non-drive-end bearing suspected. Derated to 70% pending inspection.",
        priority="high",
        raised_by="operator-01",
    )
    assert wo.id == "WO-4417"
    assert wo.status == "open"
    assert wo.pump_id == 4


def test_work_order_sequence_resets_with_the_store():
    """Reset must restore the sequence, or the next run names a different WO."""
    first = maintenance.seed()
    first.create_work_order(
        pump_id=4, title="x", description="y", priority="high", raised_by="z"
    )
    fresh = maintenance.seed()
    wo = fresh.create_work_order(
        pump_id=4, title="x", description="y", priority="high", raised_by="z"
    )
    assert wo.id == "WO-4417"


def test_created_work_order_is_listed(store):
    store.create_work_order(
        pump_id=4, title="Bearing inspection", description="d", priority="high", raised_by="op"
    )
    listed = store.list_work_orders(pump_id=4)
    assert "WO-4417" in [w.id for w in listed]


def test_work_orders_filter_by_pump_and_status(store):
    assert all(w.pump_id == 4 for w in store.list_work_orders(pump_id=4))
    assert all(w.status == "open" for w in store.list_work_orders(status="open"))


def test_due_date_is_in_the_future_when_requested(store):
    wo = store.create_work_order(
        pump_id=4,
        title="t",
        description="d",
        priority="high",
        raised_by="op",
        due_days=7,
    )
    assert wo.due is not None
    assert datetime.strptime(wo.due, "%Y-%m-%d").date() > datetime.now(UTC).date()


# --------------------------------------------------------------------------
# Dates stay fresh
# --------------------------------------------------------------------------


def test_history_dates_are_relative_to_now_not_hardcoded(store):
    """The content must not read as stale whenever the lab is delivered."""
    today = datetime.now(UTC).date()
    for record in store.records:
        assert datetime.strptime(record.date, "%Y-%m-%d").date() <= today

    prior = store.record("MR-2246")
    age_days = (today - datetime.strptime(prior.date, "%Y-%m-%d").date()).days
    assert 400 < age_days < 450, "the prior bearing failure should read as ~14 months ago"
