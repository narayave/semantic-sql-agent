"""
Build a deliberately messy payments warehouse in DuckDB.

The mess is the point. Inconsistent casing in status values, a legacy column
name nobody renamed, dates stored as text, amounts in cents, and nullable
foreign keys. A model cannot answer business questions against this schema
without a semantic layer telling it what the business terms mean.
"""

import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).parent / "payments.duckdb"
SEED = 1337

MCC = {
    5812: "Restaurants",
    5912: "Pharmacy",
    5411: "Grocery",
    7372: "Software",
    5732: "Electronics",
    8099: "Health Services",
}

# Real systems accumulate casing drift. Keep it.
STATUS_VARIANTS = ["SETTLED", "settled", "Settled"]
FAILED_VARIANTS = ["FAILED", "failed"]


def build() -> None:
    random.seed(SEED)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))

    con.execute("""
        CREATE TABLE merchants (
            mrch_id        INTEGER PRIMARY KEY,
            legal_name     VARCHAR,
            mcc            INTEGER,
            onboarded_dt   VARCHAR,      -- text, not DATE. legacy loader.
            risk_tier      VARCHAR,      -- 'low' | 'med' | 'high' | NULL
            closed_dt      VARCHAR       -- NULL means still open
        )
    """)
    con.execute("""
        CREATE TABLE txns (
            txn_id         BIGINT PRIMARY KEY,
            mrch_id        INTEGER,
            created_dt     VARCHAR,      -- text again
            txn_amt_cents  BIGINT,       -- cents, not dollars
            status         VARCHAR,      -- casing drift, see STATUS_VARIANTS
            card_brand     VARCHAR,
            is_cp          BOOLEAN       -- card present. nobody wrote this down.
        )
    """)
    con.execute("""
        CREATE TABLE disputes (
            dispute_id     BIGINT PRIMARY KEY,
            txn_id         BIGINT,
            opened_dt      VARCHAR,
            reason_cd      VARCHAR,
            outcome        VARCHAR       -- 'won' | 'lost' | NULL while open
        )
    """)
    con.execute("""
        CREATE TABLE payouts (
            payout_id      BIGINT PRIMARY KEY,
            mrch_id        INTEGER,
            paid_dt        VARCHAR,
            amt_cents      BIGINT,
            fee_cents      BIGINT
        )
    """)

    start = date(2026, 1, 1)
    merchants = []
    for i in range(1, 121):
        onboard = start + timedelta(days=random.randint(0, 200))
        closed = None
        # ~12% churned
        if random.random() < 0.12:
            closed = onboard + timedelta(days=random.randint(30, 170))
        merchants.append((
            1000 + i,
            f"{random.choice(['Blue','North','Cedar','Harbor','Vista','Union','Iron','Maple'])} "
            f"{random.choice(['Coffee','Clinic','Market','Labs','Supply','Grill','Pharmacy','Studio'])} "
            f"{random.choice(['LLC','Inc','Co'])}",
            random.choice(list(MCC)),
            onboard.isoformat(),
            random.choice(["low", "low", "med", "high", None]),
            closed.isoformat() if closed else None,
        ))
    con.executemany("INSERT INTO merchants VALUES (?,?,?,?,?,?)", merchants)

    txns, disputes = [], []
    txn_id, dispute_id = 500000, 900000
    for m in merchants:
        mid, _, _, onboard_s, tier, closed_s = m
        onboard = date.fromisoformat(onboard_s)
        closed = date.fromisoformat(closed_s) if closed_s else date(2026, 9, 30)
        span = max((closed - onboard).days, 1)
        n = random.randint(20, 220)
        # high risk merchants skew to bigger tickets and more disputes
        dispute_p = {"high": 0.055, "med": 0.02, "low": 0.008}.get(tier, 0.015)
        for _ in range(n):
            txn_id += 1
            d = onboard + timedelta(days=random.randint(0, span))
            amt = int(abs(random.gauss(4200, 2600))) + 150
            if tier == "high":
                amt = int(amt * 1.9)
            failed = random.random() < 0.06
            status = (random.choice(FAILED_VARIANTS) if failed
                      else random.choice(STATUS_VARIANTS))
            txns.append((
                txn_id, mid, d.isoformat(), amt, status,
                random.choice(["visa", "mastercard", "amex", "discover"]),
                random.random() < 0.55,
            ))
            if not failed and random.random() < dispute_p:
                dispute_id += 1
                disputes.append((
                    dispute_id, txn_id,
                    (d + timedelta(days=random.randint(2, 40))).isoformat(),
                    random.choice(["10.4", "13.1", "12.6", "4853"]),
                    random.choice(["won", "lost", "lost", None]),
                ))
    con.executemany("INSERT INTO txns VALUES (?,?,?,?,?,?,?)", txns)
    con.executemany("INSERT INTO disputes VALUES (?,?,?,?,?)", disputes)

    payouts = []
    payout_id = 700000
    for m in merchants:
        mid = m[0]
        for _ in range(random.randint(3, 28)):
            payout_id += 1
            amt = random.randint(20000, 900000)
            payouts.append((
                payout_id, mid,
                (start + timedelta(days=random.randint(10, 270))).isoformat(),
                amt, int(amt * random.uniform(0.019, 0.031)),
            ))
    con.executemany("INSERT INTO payouts VALUES (?,?,?,?,?)", payouts)

    con.close()
    print(f"built {DB_PATH}")
    print(f"  merchants={len(merchants)} txns={len(txns)} "
          f"disputes={len(disputes)} payouts={len(payouts)}")


if __name__ == "__main__":
    build()
