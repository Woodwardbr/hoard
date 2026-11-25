from __future__ import annotations

from datetime import date

import pytest

from hoard import splits


@pytest.fixture
def trip(make_account, make_txn):
    make_account("acc1", kind="credit")
    make_txn("t1", "acc1", "2026-07-01", 90.0, name="Dinner")
    make_txn("t2", "acc1", "2026-07-02", 40.0, name="Taxi", merchant="Uber")
    make_txn("t3", "acc1", "2026-07-03", -25.0, name="Refund")
    group_id = splits.create_group("Tahoe", None, None, ["Alex", "Sam"])
    ids = {p["name"]: p["person_id"] for p in splits.list_people()}
    return group_id, ids


def test_compute_shares_even_rounds_to_exact_total():
    result = splits.compute_shares(100, "even", {"me": 1, "a": 1, "b": 1})
    assert round(sum(result.values()), 2) == 100.00
    assert sorted(result.values()) == [33.33, 33.33, 33.34]


def test_compute_shares_weights():
    assert splits.compute_shares(100, "shares", {"me": 2, "a": 1, "b": 1}) == {
        "me": 50.0,
        "a": 25.0,
        "b": 25.0,
    }


def test_compute_shares_exact_must_sum_to_amount():
    with pytest.raises(ValueError):
        splits.compute_shares(100, "exact", {"me": 60, "a": 30})
    assert splits.compute_shares(100, "exact", {"me": 60, "a": 40}) == {"me": 60.0, "a": 40.0}


def test_compute_shares_rejects_bad_input():
    with pytest.raises(ValueError):
        splits.compute_shares(100, "shares", {"me": 0, "a": 1})
    with pytest.raises(ValueError):
        splits.compute_shares(100, "even", {})


def test_even_split_balances(trip):
    group_id, ids = trip
    splits.assign_expenses(
        group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1, ids["Sam"]: 1}
    )
    bal = splits.group_balances(group_id)
    by_name = {p["name"]: p for p in bal["people"]}
    assert by_name["Alex"]["owed"] == 30.0
    assert by_name["Sam"]["owed"] == 30.0
    assert bal["my_share"] == 30.0
    assert bal["total_spent"] == 90.0


def test_repayment_reduces_outstanding(trip):
    group_id, ids = trip
    splits.assign_expenses(
        group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1, ids["Sam"]: 1}
    )
    splits.record_repayment(group_id, ids["Alex"], 10.0, date(2026, 7, 5))
    by_name = {p["name"]: p for p in splits.group_balances(group_id)["people"]}
    assert by_name["Alex"]["repaid"] == 10.0
    assert by_name["Alex"]["outstanding"] == 20.0
    assert by_name["Sam"]["outstanding"] == 30.0


def test_resplit_replaces_old_shares(trip):
    group_id, ids = trip
    splits.assign_expenses(
        group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1, ids["Sam"]: 1}
    )
    splits.assign_expenses(group_id, ["t1"], "shares", {"me": 1, ids["Alex"]: 2})
    by_name = {p["name"]: p for p in splits.group_balances(group_id)["people"]}
    assert by_name["Alex"]["owed"] == 60.0
    assert by_name["Sam"]["owed"] == 0.0
    assert len(splits.get_group(group_id)["expenses"]) == 1


def test_transaction_cannot_be_in_two_groups(trip):
    group_id, ids = trip
    other = splits.create_group("Other", None, None, ["Alex"])
    splits.assign_expenses(group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1})
    with pytest.raises(splits.SplitConflict):
        splits.assign_expenses(other, ["t1"], "even", {"me": 1, ids["Alex"]: 1})


def test_non_member_participant_rejected(trip):
    group_id, _ = trip
    with pytest.raises(ValueError):
        splits.assign_expenses(group_id, ["t1"], "even", {"me": 1, "nobody": 1})


def test_exact_with_two_transactions_rejected(trip):
    group_id, ids = trip
    with pytest.raises(ValueError):
        splits.assign_expenses(
            group_id, ["t1", "t2"], "exact", {"me": 50, ids["Alex"]: 40}
        )


def test_failed_assign_writes_nothing(trip):
    group_id, ids = trip
    with pytest.raises(LookupError):
        splits.assign_expenses(
            group_id, ["t1", "missing"], "even", {"me": 1, ids["Alex"]: 1}
        )
    assert splits.get_group(group_id)["expenses"] == []


def test_refund_cannot_be_assigned(trip):
    group_id, ids = trip
    with pytest.raises(ValueError):
        splits.assign_expenses(group_id, ["t3"], "even", {"me": 1, ids["Alex"]: 1})


def test_search_transactions(trip):
    group_id, ids = trip
    rows = splits.search_transactions(date(2026, 7, 1), date(2026, 7, 31))
    assert [r["transaction_id"] for r in rows] == ["t2", "t1"]  # refund skipped, newest first

    only_day = splits.search_transactions(date(2026, 7, 1), date(2026, 7, 1))
    assert [r["transaction_id"] for r in only_day] == ["t1"]

    by_merchant = splits.search_transactions(date(2026, 7, 1), date(2026, 7, 31), q="uber")
    assert [r["transaction_id"] for r in by_merchant] == ["t2"]

    splits.assign_expenses(group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1})
    rows = splits.search_transactions(date(2026, 7, 1), date(2026, 7, 31))
    assigned = {r["transaction_id"]: r["group_name"] for r in rows}
    assert assigned == {"t1": "Tahoe", "t2": None}


def test_unassign_expense(trip):
    group_id, ids = trip
    splits.assign_expenses(group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1})
    splits.unassign_expense(group_id, "t1")
    by_name = {p["name"]: p for p in splits.group_balances(group_id)["people"]}
    assert by_name["Alex"]["outstanding"] == 0.0
    with pytest.raises(LookupError):
        splits.unassign_expense(group_id, "t1")


def test_repayment_requires_member(trip):
    group_id, _ = trip
    with pytest.raises(ValueError):
        splits.record_repayment(group_id, "nobody", 5.0, date(2026, 7, 5))


def test_list_groups_total_outstanding(trip):
    group_id, ids = trip
    splits.assign_expenses(group_id, ["t1"], "even", {"me": 1, ids["Alex"]: 1})
    assert splits.list_groups()[0]["total_outstanding"] == 45.0


def test_splits_page_and_dashboard_api(make_account, make_txn):
    from fastapi.testclient import TestClient

    from hoard.app import app

    make_account("acc1", kind="credit")
    make_txn("t1", "acc1", "2026-07-01", 90.0, name="Dinner")
    client = TestClient(app)

    page = client.get("/splits")
    assert page.status_code == 200
    assert 'id="splits-page"' in page.text
    assert 'href="/splits"' in page.text

    gid = client.post(
        "/api/splits/groups", json={"name": "Tahoe", "members": ["Alex"]}
    ).json()["group_id"]
    alex = client.get(f"/api/splits/groups/{gid}").json()["members"][0]["person_id"]

    found = client.get(
        "/api/splits/transactions", params={"start": "2026-07-01", "end": "2026-07-31"}
    )
    assert [r["transaction_id"] for r in found.json()] == ["t1"]

    body = {
        "transaction_ids": ["t1"],
        "split_mode": "even",
        "shares": [{"participant": "me"}, {"participant": alex}],
    }
    assert client.post(f"/api/splits/groups/{gid}/expenses", json=body).status_code == 200
    other = client.post("/api/splits/groups", json={"name": "Other", "members": ["Alex"]}).json()
    # Alex is the same person in both groups, so only the "already assigned" check can fail.
    clash = client.post(f"/api/splits/groups/{other['group_id']}/expenses", json=body)
    assert clash.status_code == 409
    repaid = client.post(
        f"/api/splits/groups/{gid}/repayments", json={"person_id": alex, "amount": 15}
    )
    assert repaid.status_code == 200
    detail = client.get(f"/api/splits/groups/{gid}").json()
    assert detail["balances"]["people"][0]["outstanding"] == 30.0
    assert client.post(
        f"/api/splits/groups/{gid}/repayments", json={"person_id": "nobody", "amount": 5}
    ).status_code == 400
    assert client.get("/api/splits/groups/nope").status_code == 404
    repayment_id = repaid.json()["repayment_id"]
    assert client.delete(f"/api/splits/repayments/{repayment_id}").status_code == 200
    assert client.delete(f"/api/splits/groups/{gid}/expenses/t1").status_code == 200
    assert client.delete(f"/api/splits/groups/{gid}").status_code == 200
    assert gid not in [g["group_id"] for g in client.get("/api/splits/groups").json()]
