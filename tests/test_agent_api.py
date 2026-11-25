from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from hoard import agent_api, plaid
from hoard.app import app
from hoard.db import get_conn

TOKEN = "test-token"
AUTH = {"X-Muse-Token": TOKEN}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "settings",
        dataclasses.replace(agent_api.settings, agent_api_token=TOKEN, agent_allow_writes=True),
    )
    return TestClient(agent_api.agent_app)


def make_loan(loan_id: str, name: str) -> None:
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name) VALUES (?, ?)", (loan_id, name))


def loan_payments() -> list[tuple]:
    with get_conn() as conn:
        rows = conn.execute("SELECT loan_id, amount FROM loan_payments").fetchall()
    return [tuple(r) for r in rows]


def test_rejects_missing_and_wrong_token(client):
    assert client.get("/api/muse/health").status_code == 401
    assert client.get("/api/muse/health", headers={"X-Muse-Token": "nope"}).status_code == 401
    assert client.get("/api/muse/health", headers=AUTH).status_code == 200


def test_unconfigured_token_returns_503(monkeypatch):
    monkeypatch.setattr(
        agent_api, "settings", dataclasses.replace(agent_api.settings, agent_api_token="")
    )
    assert TestClient(agent_api.agent_app).get("/api/muse/health", headers=AUTH).status_code == 503


def test_agent_app_serves_only_muse_routes(client):
    for path in ("/", "/api/budgets", "/docs", "/openapi.json"):
        assert client.get(path, headers=AUTH).status_code == 404, path
    assert client.delete("/api/items/item1", headers=AUTH).status_code == 404


def test_dashboard_app_does_not_serve_muse_routes():
    assert TestClient(app).get("/api/muse/health", headers=AUTH).status_code == 404


def test_spending_summary_rejects_bad_period(client):
    resp = client.get("/api/muse/spending-summary", params={"period": "2026-13"}, headers=AUTH)
    assert resp.status_code == 400


def test_payoff_balances_skips_items_without_credit_or_loan(client, make_account, monkeypatch):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_account("brk", kind="investment", item_id="broker", institution="Broker")
    queried: list[str] = []

    def fake_liabilities(token: str):
        queried.append(token)
        return [
            plaid.CreditLiability(
                account_id="card",
                last_statement_balance=500.0,
                last_statement_issue_date="2026-09-01",
                minimum_payment_amount=25.0,
                next_payment_due_date="2026-09-25",
                last_payment_amount=None,
                last_payment_date=None,
            )
        ], []

    monkeypatch.setattr(plaid, "get_liabilities", fake_liabilities)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert len(queried) == 1
    assert body["errors"] == []
    assert [c["account_id"] for c in body["credit_cards"]] == ["card"]
    assert body["credit_cards"][0]["statement_balance"] == 500.0
    assert body["credit_cards"][0]["payoff_amount"] == 500.0
    assert body["credit_cards"][0]["balance_source"] == "statement"


def plaid_error(code: str) -> Exception:
    exc = plaid.plaid.ApiException(status=400, reason="Bad Request")
    exc.body = json.dumps({"error_code": code, "error_message": "nope"})
    return exc


def test_payoff_balances_falls_back_to_current_without_consent(
    client, make_account, make_balance, monkeypatch
):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_balance("card", "2026-09-01", 900.0)
    make_balance("card", "2026-09-29", 1234.5)

    def no_consent(token: str):
        raise plaid_error("ADDITIONAL_CONSENT_REQUIRED")

    monkeypatch.setattr(plaid, "get_liabilities", no_consent)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["errors"] == []
    card = body["credit_cards"][0]
    assert card["payoff_amount"] == 1234.5
    assert card["balance_source"] == "current"
    assert card["balance_as_of"] == "2026-09-29"
    assert card["statement_balance"] is None
    assert card["due_date"] is None


def test_payoff_balances_reports_unexpected_errors_but_keeps_card(
    client, make_account, make_balance, monkeypatch
):
    make_account("card", kind="credit", item_id="bank", institution="Bank")
    make_balance("card", "2026-09-29", 80.0)

    def broken(token: str):
        raise plaid_error("ITEM_LOGIN_REQUIRED")

    monkeypatch.setattr(plaid, "get_liabilities", broken)
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["errors"] == [{"institution": "Bank", "error_code": "ITEM_LOGIN_REQUIRED"}]
    assert body["credit_cards"][0]["payoff_amount"] == 80.0


def test_payoff_balances_includes_tracked_loans(client):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, min_payment, next_due_date, auto_pay) "
            "VALUES ('l1', 'Direct Loan', 150.0, '2099-10-15', 1)"
        )
    body = client.get("/api/muse/payoff-balances", headers=AUTH).json()

    assert body["tracked_loans"] == [
        {
            "loan_id": "l1",
            "name": "Direct Loan",
            "monthly_payment": 150.0,
            "next_due_date": "2099-10-15",
            "current_balance": None,
            "interest_rate": None,
            "auto_pay": True,
            "status": "Scheduled",
        }
    ]


def test_record_payment_unknown_account_404(client):
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "missing", "amount": 10}, headers=AUTH
    )
    assert resp.status_code == 404


def test_record_payment_rejects_non_positive_amount(client, make_account):
    make_account("loan", kind="loan")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "loan", "amount": -5}, headers=AUTH
    )
    assert resp.status_code == 422


def test_record_payment_to_loan_account_is_combined(client, make_account):
    make_account("aid", kind="loan", name="Student Loan Servicer")
    make_loan("l1", "1-01 Direct Loan - Subsidized")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "aid", "amount": 300}, headers=AUTH
    )
    assert resp.json()["stored"] is True
    assert loan_payments() == [(None, 300.0)]


def test_record_payment_with_loan_id(client, make_account):
    make_account("aid", kind="loan")
    make_loan("l1", "1-01 Direct Loan - Subsidized")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "l1"},
        headers=AUTH,
    )
    assert resp.json()["loan"] == "1-01 Direct Loan - Subsidized"
    assert loan_payments() == [("l1", 120.0)]


def test_record_payment_unknown_loan_id_404(client, make_account):
    make_account("aid", kind="loan")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "nope"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert loan_payments() == []


def test_record_payment_to_card_not_stored(client, make_account):
    make_account("card", kind="credit")
    resp = client.post(
        "/api/muse/record-payment", json={"account_id": "card", "amount": 50}, headers=AUTH
    )
    assert resp.json()["stored"] is False
    assert loan_payments() == []


def test_record_payment_accepts_autopay_loan(client, make_account):
    """Autopay can fail, so manual payments on autopay loans are recorded."""
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'Direct', 1)")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "l1"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    assert len(loan_payments()) == 1


def test_record_payment_refuses_duplicate(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'Direct', 1)")
    body = {"account_id": "aid", "amount": 120, "loan_id": "l1", "payment_date": "2026-11-09"}
    first = client.post("/api/muse/record-payment", json=body, headers=AUTH)
    assert first.status_code == 200
    dup = client.post("/api/muse/record-payment", json=body, headers=AUTH)
    assert dup.status_code == 409
    assert dup.json()["detail"]["existing_payment_id"] == first.json()["payment_id"]
    assert len(loan_payments()) == 1


def test_record_payment_same_day_different_loans_both_stored(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'A', 1)")
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l2', 'B', 1)")
    for loan_id in ("l1", "l2"):
        resp = client.post(
            "/api/muse/record-payment",
            json={"account_id": "aid", "amount": 120, "loan_id": loan_id,
                  "payment_date": "2026-11-09"},
            headers=AUTH,
        )
        assert resp.status_code == 200
    assert len(loan_payments()) == 2


def test_record_payment_combined_duplicate_refused(client, make_account):
    make_account("aid", kind="loan")
    body = {"account_id": "aid", "amount": 300, "payment_date": "2026-11-09"}
    assert client.post("/api/muse/record-payment", json=body, headers=AUTH).status_code == 200
    assert client.post("/api/muse/record-payment", json=body, headers=AUTH).status_code == 409
    assert len(loan_payments()) == 1


def test_record_payment_updates_loan_balance_and_status(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, auto_pay, current_balance, status) "
            "VALUES ('l1', 'Direct', 1, 5000, 'Scheduled')"
        )
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "loan_id": "l1",
              "payment_date": "2026-11-09", "new_balance": 4910.5, "status": "Paid"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["updated_loan"]["current_balance"] == 4910.5
    row = loan_row("l1")
    assert row["current_balance"] == 4910.5
    assert row["status"] == "Paid"
    with get_conn() as conn:
        snap = conn.execute(
            "SELECT balance FROM loan_balance_snapshots WHERE loan_id='l1' "
            "AND snapshot_date='2026-11-09'"
        ).fetchone()
    assert snap["balance"] == 4910.5


def test_record_payment_new_balance_without_loan_id_rejected(client, make_account):
    make_account("aid", kind="loan")
    resp = client.post(
        "/api/muse/record-payment",
        json={"account_id": "aid", "amount": 120, "new_balance": 100},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert loan_payments() == []


def test_record_payment_duplicate_does_not_touch_loan(client, make_account):
    make_account("aid", kind="loan")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, auto_pay, current_balance) "
            "VALUES ('l1', 'Direct', 1, 5000)"
        )
    body = {"account_id": "aid", "amount": 120, "loan_id": "l1", "payment_date": "2026-11-09"}
    assert client.post("/api/muse/record-payment", json=body, headers=AUTH).status_code == 200
    dup = client.post(
        "/api/muse/record-payment", json=body | {"new_balance": 1}, headers=AUTH
    )
    assert dup.status_code == 409
    assert loan_row("l1")["current_balance"] == 5000


def test_record_payment_loan_id_without_account_id(client):
    """Tracked loans have no Plaid account, so loan_id alone is enough."""
    with get_conn() as conn:
        conn.execute("INSERT INTO loans (loan_id, name, auto_pay) VALUES ('l1', 'Direct', 1)")
    resp = client.post(
        "/api/muse/record-payment",
        json={"loan_id": "l1", "amount": 120, "payment_date": "2026-11-09"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert loan_payments() == [("l1", 120.0)]
    with get_conn() as conn:
        source = conn.execute("SELECT source FROM loan_payments").fetchone()["source"]
    assert source == "Agent payoff (Direct)"


def test_record_payment_needs_account_or_loan(client):
    resp = client.post("/api/muse/record-payment", json={"amount": 120}, headers=AUTH)
    assert resp.status_code == 422


def loan_row(loan_id: str) -> dict:
    with get_conn() as conn:
        return dict(conn.execute("SELECT * FROM loans WHERE loan_id = ?", (loan_id,)).fetchone())


def snapshots(loan_id: str) -> list[float]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT balance FROM loan_balance_snapshots WHERE loan_id = ?", (loan_id,)
        ).fetchall()
    return [r["balance"] for r in rows]


@pytest.fixture
def two_loans():
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO loans (loan_id, name, current_balance, min_payment, next_due_date, "
            "interest_rate, auto_pay, notes) VALUES "
            "('l1', 'Loan 1', 5000, 50, '2026-10-15', 5.5, 1, 'keep me'), "
            "('l2', 'Loan 2', 8000, 80, '2026-10-15', 6.5, 1, NULL)"
        )


def test_update_loans_changes_only_sent_fields(client, two_loans):
    resp = client.post(
        "/api/muse/update-loans",
        json={"updates": [
            {"loan_id": "l1", "current_balance": 4950.12, "next_due_date": "2026-11-15"},
            {"loan_id": "l2", "min_payment": 81.25},
        ]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert [ln["loan_id"] for ln in resp.json()["loans"]] == ["l1", "l2"]

    l1, l2 = loan_row("l1"), loan_row("l2")
    assert (l1["current_balance"], l1["next_due_date"]) == (4950.12, "2026-11-15")
    assert (l1["min_payment"], l1["interest_rate"], l1["notes"]) == (50, 5.5, "keep me")
    assert (l2["min_payment"], l2["current_balance"]) == (81.25, 8000)
    assert snapshots("l1") == [4950.12]
    assert snapshots("l2") == []


def test_update_loans_is_all_or_nothing(client, two_loans):
    resp = client.post(
        "/api/muse/update-loans",
        json={"updates": [
            {"loan_id": "l1", "current_balance": 1.0},
            {"loan_id": "missing", "current_balance": 2.0},
        ]},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert loan_row("l1")["current_balance"] == 5000
    assert snapshots("l1") == []


@pytest.mark.parametrize(
    "update",
    [
        {"loan_id": "l1"},                                   # nothing to update
        {"loan_id": "l1", "current_balance": None},          # explicit null
        {"loan_id": "l1", "current_balance": -1},
        {"loan_id": "l1", "interest_rate": 101},
        {"loan_id": "l1", "next_due_date": "2026-02-30"},
        {"loan_id": "l1", "status": "Closed"},
        {"loan_id": "l1", "auto_pay": False},                # not updatable
        {"loan_id": "l1", "name": "Renamed"},                # not updatable
    ],
)
def test_update_loans_rejects_invalid(client, two_loans, update):
    resp = client.post("/api/muse/update-loans", json={"updates": [update]}, headers=AUTH)
    assert resp.status_code == 422
    assert loan_row("l1")["current_balance"] == 5000


def test_update_loans_rejects_duplicates_and_empty(client, two_loans):
    dup = {"updates": [{"loan_id": "l1", "min_payment": 1}, {"loan_id": "l1", "min_payment": 2}]}
    assert client.post("/api/muse/update-loans", json=dup, headers=AUTH).status_code == 400
    empty = {"updates": []}
    assert client.post("/api/muse/update-loans", json=empty, headers=AUTH).status_code == 422
    assert loan_row("l1")["min_payment"] == 50


def test_update_loans_requires_token(client, two_loans):
    body = {"updates": [{"loan_id": "l1", "min_payment": 1}]}
    assert client.post("/api/muse/update-loans", json=body).status_code == 401


def _months_ago(n: int) -> str:
    from datetime import date

    today = date.today()
    total = today.year * 12 + today.month - 1 - n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def test_spending_summary_compares_month_to_other_months(client, make_account, make_txn):
    make_account("cc", kind="credit")
    m3, m2, m1 = _months_ago(3), _months_ago(2), _months_ago(1)
    make_txn("a", "cc", f"{m3}-01", 100.0, override="Groceries")
    make_txn("b", "cc", f"{m3}-06", 60.0, override="Restaurants")
    make_txn("c", "cc", f"{m2}-05", 200.0, override="Groceries")
    make_txn("d", "cc", f"{m1}-05", 450.0, override="Groceries", merchant="Costco")
    make_txn("e", "cc", f"{m1}-20", 150.0, override="Groceries")

    body = client.get("/api/muse/spending-summary", params={"period": m1}, headers=AUTH).json()

    assert body["month_complete"] is True
    assert body["baseline_months"] == 2  # m3 and m2; the month under review is excluded
    cats = {c["category"]: c for c in body["categories"]}
    groceries = cats[next(k for k in cats if "Grocer" in k)]
    assert (groceries["spent"], groceries["avg_monthly"]) == (600.0, 150.0)
    assert (groceries["difference"], groceries["pct_of_avg"]) == (450.0, 400.0)
    assert body["categories"][0] is not None and body["categories"][0]["difference"] == 450.0
    dining = [c for c in body["categories"] if c["spent"] == 0]
    assert dining and dining[0]["avg_monthly"] == 30.0
    assert [t["amount"] for t in body["largest_transactions"]] == [450.0, 150.0]
    assert body["largest_transactions"][0]["merchant"] == "Costco"


def test_spending_summary_current_month_is_incomplete(client):
    body = client.get(
        "/api/muse/spending-summary", params={"period": _months_ago(0)}, headers=AUTH
    ).json()
    assert body["month_complete"] is False
    assert body["categories"] == []


# --- Splits ------------------------------------------------------------------


def test_muse_split_workflow(client, make_account, make_txn):
    make_account("acc1", kind="credit")
    make_txn("t1", "acc1", "2026-07-01", 90.0, name="Dinner")
    make_txn("t2", "acc1", "2026-07-02", 40.0, name="Taxi", merchant="Uber")

    found = client.get(
        "/api/muse/transactions",
        params={"start": "2026-07-01", "end": "2026-07-31", "q": "uber"},
        headers=AUTH,
    )
    assert found.status_code == 200
    assert [r["transaction_id"] for r in found.json()] == ["t2"]

    created = client.post(
        "/api/muse/split-groups",
        json={"name": "Tahoe", "members": ["Alex", "Sam"]},
        headers=AUTH,
    )
    assert created.status_code == 200
    gid = created.json()["group_id"]

    assigned = client.post(
        f"/api/muse/split-groups/{gid}/expenses",
        json={
            "transaction_ids": ["t1"],
            "split_mode": "even",
            "shares": [
                {"participant": "me"},
                {"participant": "alex"},
                {"participant": "Sam"},
            ],
        },
        headers=AUTH,
    )
    assert assigned.status_code == 200, assigned.text

    repaid = client.post(
        f"/api/muse/split-groups/{gid}/repayments",
        json={"person": "Alex", "amount": 10, "paid_date": "2026-07-05"},
        headers=AUTH,
    )
    assert repaid.status_code == 200

    detail = client.get(f"/api/muse/split-groups/{gid}", headers=AUTH).json()
    by_name = {p["name"]: p for p in detail["balances"]["people"]}
    assert by_name["Alex"]["outstanding"] == 20.0
    assert by_name["Sam"]["outstanding"] == 30.0
    listed = client.get("/api/muse/split-groups", headers=AUTH).json()
    assert listed[0]["total_outstanding"] == 50.0

    added = client.post(
        f"/api/muse/split-groups/{gid}/members", json={"names": ["Jo"]}, headers=AUTH
    )
    assert added.status_code == 200

    removed = client.delete(f"/api/muse/split-groups/{gid}/expenses/t1", headers=AUTH)
    assert removed.status_code == 200
    detail = client.get(f"/api/muse/split-groups/{gid}", headers=AUTH).json()
    assert detail["expenses"] == []


def test_muse_split_errors(client, make_account, make_txn):
    make_account("acc1", kind="credit")
    make_txn("t1", "acc1", "2026-07-01", 90.0)
    gid = client.post(
        "/api/muse/split-groups", json={"name": "A", "members": ["Alex"]}, headers=AUTH
    ).json()["group_id"]
    other = client.post(
        "/api/muse/split-groups", json={"name": "B", "members": ["Alex"]}, headers=AUTH
    ).json()["group_id"]

    body = {
        "transaction_ids": ["t1"],
        "split_mode": "even",
        "shares": [{"participant": "me"}, {"participant": "Alex"}],
    }
    url = f"/api/muse/split-groups/{gid}/expenses"
    assert client.post(url, json=body, headers=AUTH).status_code == 200
    # Already in another group.
    resp = client.post(f"/api/muse/split-groups/{other}/expenses", json=body, headers=AUTH)
    assert resp.status_code == 409
    # Unknown participant, group and transaction.
    bad = {**body, "shares": [{"participant": "Stranger"}]}
    assert client.post(url, json=bad, headers=AUTH).status_code == 404
    assert client.get("/api/muse/split-groups/nope", headers=AUTH).status_code == 404
    missing = {**body, "transaction_ids": ["nope"]}
    other_url = f"/api/muse/split-groups/{other}/expenses"
    assert client.post(other_url, json=missing, headers=AUTH).status_code == 404


def test_muse_split_routes_require_token_and_reject_extras(client):
    assert client.get("/api/muse/split-groups").status_code == 401
    assert client.get("/api/muse/transactions?start=2026-07-01&end=2026-07-31").status_code == 401
    resp = client.post(
        "/api/muse/split-groups", json={"name": "A", "bogus": 1}, headers=AUTH
    )
    assert resp.status_code == 422


def test_muse_cannot_delete_groups_or_repayments(client):
    gid = client.post("/api/muse/split-groups", json={"name": "A"}, headers=AUTH).json()["group_id"]
    assert client.delete(f"/api/muse/split-groups/{gid}", headers=AUTH).status_code in (404, 405)
    assert client.delete("/api/muse/split-repayments/x", headers=AUTH).status_code in (404, 405)
    assert client.delete("/api/muse/split-people/x", headers=AUTH).status_code in (404, 405)
    assert client.get(f"/api/muse/split-groups/{gid}", headers=AUTH).status_code == 200



# --- Generic agent API ---------------------------------------------------------

AGENT_AUTH = {"X-Agent-Token": TOKEN}


def test_agent_prefix_and_header(client):
    assert client.get("/api/agent/health", headers=AGENT_AUTH).json()["writes_allowed"] is True
    assert client.get("/api/agent/health").status_code == 401


def test_writes_disabled_returns_403(client, monkeypatch, make_account):
    monkeypatch.setattr(
        agent_api, "settings", dataclasses.replace(agent_api.settings, agent_allow_writes=False)
    )
    make_account("loan", kind="loan")
    resp = client.post(
        "/api/agent/record-payment", json={"account_id": "loan", "amount": 10}, headers=AGENT_AUTH
    )
    assert resp.status_code == 403
    assert "AGENT_ALLOW_WRITES" in resp.json()["detail"]
    # Reads still work.
    assert client.get("/api/agent/accounts", headers=AGENT_AUTH).status_code == 200


def test_read_endpoints(client, make_account, make_balance):
    make_account("chk", kind="depository", name="Checking")
    make_balance("chk", "2026-08-29", 1500.0)
    make_account("cc", kind="credit", name="Card")
    make_balance("cc", "2026-08-29", 500.0)

    accts = client.get("/api/agent/accounts", headers=AGENT_AUTH).json()
    assert {a["name"]: a["balance"] for a in accts} == {"Checking": 1500.0, "Card": 500.0}

    nw = client.get("/api/agent/net-worth", headers=AGENT_AUTH).json()
    assert nw["latest"]["net"] == 1000.0 and "history" not in nw
    nw = client.get("/api/agent/net-worth", params={"history": True}, headers=AGENT_AUTH).json()
    assert nw["history"]

    port = client.get("/api/agent/portfolio", headers=AGENT_AUTH).json()
    assert port == [{"sector": "Cash", "total": 1500.0, "pct": 100.0, "assets": 1}]

    assert client.get("/api/agent/budgets", headers=AGENT_AUTH).json() == []
    assert client.get("/api/agent/budgets", params={"period": "bad"},
                      headers=AGENT_AUTH).status_code == 400
    assert "total_balance" in client.get("/api/agent/debt-summary", headers=AGENT_AUTH).json()
