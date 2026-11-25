"""Fill a database with made-up accounts and six months of activity.

For screenshots and for trying Hoard without connecting a bank:

    HOARD_HOME=/tmp/hoard-demo uv run python scripts/demo_data.py
    HOARD_HOME=/tmp/hoard-demo PLAID_CLIENT_ID=demo PLAID_SECRET=demo uv run hoard

Everything here is fictional. Refuses to touch a database that already has data.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta

from hoard import analytics, db
from hoard.db import get_conn

rng = random.Random(42)
TODAY = date.today()
START = (TODAY.replace(day=1) - timedelta(days=170)).replace(day=1)

ACCOUNTS = [
    # id, item, institution, name, mask, kind, subtype
    ("chk", "bank", "Riverside Bank", "Everyday Checking", "4821", "depository", "checking"),
    ("sav", "bank", "Riverside Bank", "High-Yield Savings", "9034", "depository", "savings"),
    ("visa", "card", "Summit Card Co.", "Summit Rewards Visa", "1177", "credit", "credit card"),
    ("brk", "invest", "Northwind Brokerage", "Individual Brokerage", "5530", "investment",
     "brokerage"),
    ("ira", "invest", "Northwind Brokerage", "Roth IRA", "5531", "investment", "roth"),
]

# category, merchants, monthly count, typical amount
SPENDING = [
    ("FOOD_AND_DRINK", ["Corner Market", "Green Leaf Grocery", "Blue Door Cafe", "Pasta House"],
     14, 38),
    ("TRANSPORTATION", ["City Transit", "QuickFuel"], 5, 42),
    ("GENERAL_MERCHANDISE", ["Everything Store", "Book Nook", "Hardware Hut"], 5, 55),
    ("ENTERTAINMENT", ["StreamFlix", "Cinema 8", "Tunes Unlimited"], 3, 18),
    ("PERSONAL_CARE", ["Fresh Cuts", "Wellness Pharmacy"], 2, 30),
    ("TRAVEL", ["Skyline Air", "Harbor Hotel"], 1, 260),
]

HOLDINGS = [
    # account, security_id, ticker, name, type, sector, quantity, price
    ("brk", "s-vti", "VTI", "Vanguard Total Stock Market ETF", "etf", None, 48, 290.0),
    ("brk", "s-vxus", "VXUS", "Vanguard Total International Stock ETF", "etf", None, 85, 66.0),
    ("brk", "s-aapl", "AAPL", "Apple Inc.", "equity", "Electronic Technology", 20, 230.0),
    ("brk", "s-jnj", "JNJ", "Johnson & Johnson", "equity", "Health Technology", 15, 160.0),
    ("ira", "s-bnd", "BND", "Vanguard Total Bond Market ETF", "etf", None, 120, 73.0),
    ("ira", "s-vti", "VTI", "Vanguard Total Stock Market ETF", "etf", None, 30, 290.0),
]


def main() -> None:
    with get_conn() as conn:
        if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            sys.exit(f"{db.settings.db_path} already has data; use an empty HOARD_HOME.")

        for _id, item, inst, *_ in ACCOUNTS:
            conn.execute(
                "INSERT OR IGNORE INTO items (item_id, access_token, institution)"
                " VALUES (?, '', ?)",
                (item, inst),
            )
        for acct_id, item, _inst, name, mask, kind, subtype in ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts (account_id, item_id, name, mask, kind, subtype,"
                " iso_currency) VALUES (?, ?, ?, ?, ?, ?, 'USD')",
                (acct_id, item, name, mask, kind, subtype),
            )

        # Transactions: card spending, rent and paychecks.
        n = 0
        d = START
        while d <= TODAY:
            if d.day == 1:
                for cat, merchants, count, typical in SPENDING:
                    for _ in range(max(0, int(rng.gauss(count, count / 4)))):
                        day = d + timedelta(days=rng.randrange(28))
                        if day > TODAY:
                            continue
                        n += 1
                        merchant = rng.choice(merchants)
                        amount = round(max(3.0, rng.gauss(typical, typical / 3)), 2)
                        conn.execute(
                            "INSERT INTO transactions (transaction_id, account_id, date, name,"
                            " merchant, amount, iso_currency, category_primary)"
                            " VALUES (?, 'visa', ?, ?, ?, ?, 'USD', ?)",
                            (f"t{n}", day.isoformat(), merchant.upper(), merchant, amount, cat),
                        )
                n += 1
                conn.execute(
                    "INSERT INTO transactions (transaction_id, account_id, date, name, merchant,"
                    " amount, iso_currency, category_primary)"
                    " VALUES (?, 'chk', ?, 'MAPLE PROPERTY MGMT RENT', 'Maple Property', 1650,"
                    " 'USD', 'RENT_AND_UTILITIES')",
                    (f"t{n}", (d + timedelta(days=1)).isoformat()),
                )
                n += 1
                conn.execute(
                    "INSERT INTO transactions (transaction_id, account_id, date, name, merchant,"
                    " amount, iso_currency, category_primary)"
                    " VALUES (?, 'chk', ?, 'CITY POWER & WATER', 'City Utilities', ?, 'USD',"
                    " 'RENT_AND_UTILITIES')",
                    (f"t{n}", (d + timedelta(days=12)).isoformat(), round(rng.uniform(90, 150), 2)),
                )
            d += timedelta(days=1)

        # Weekly balance snapshots with gentle trends.
        week = 0
        d = START
        while d <= TODAY:
            t = week / 26
            balances = {
                "chk": 3200 + rng.uniform(-600, 600),
                "sav": 12000 + 4000 * t,
                "visa": 900 + rng.uniform(-400, 700),
                "brk": 24000 * (1 + 0.09 * t + rng.uniform(-0.02, 0.02)),
                "ira": 14500 * (1 + 0.06 * t + rng.uniform(-0.015, 0.015)),
            }
            for acct, bal in balances.items():
                conn.execute(
                    "INSERT INTO balances (account_id, snapshot_date, current) VALUES (?, ?, ?)",
                    (acct, d.isoformat(), round(bal, 2)),
                )
            d += timedelta(days=7)
            week += 1

        for acct, sid, ticker, name, typ, sector, qty, price in HOLDINGS:
            conn.execute(
                "INSERT OR IGNORE INTO securities (security_id, ticker, name, type, sector)"
                " VALUES (?, ?, ?, ?, ?)",
                (sid, ticker, name, typ, sector),
            )
            conn.execute(
                "INSERT INTO holdings (account_id, security_id, quantity, institution_price,"
                " institution_value, snapshot_date) VALUES (?, ?, ?, ?, ?, ?)",
                (acct, sid, qty, price, round(qty * price, 2), TODAY.isoformat()),
            )

    for category, limit in [("Food & Drink", 600), ("Merchandise", 250), ("Health & Fitness", 100),
                            ("Transportation", 220)]:
        analytics.upsert_budget(category, limit)

    for name, rate, minimum, balance in [
        ("Federal Direct Loan (Subsidized)", 4.99, 145.0, 9800.0),
        ("Federal Direct Loan (Unsubsidized)", 6.54, 210.0, 15400.0),
    ]:
        analytics.upsert_loan(
            None, name, "Direct", rate, minimum, balance,
            (TODAY.replace(day=1) + timedelta(days=45)).replace(day=15), True, "Scheduled", None,
        )

    print(f"Demo data written to {db.settings.db_path}")


if __name__ == "__main__":
    main()
