"""
PEAR report financial math -- kept as its own small, pure module (no PDF
parsing, no Flask) so every formula can be unit-tested directly against
Brian's worked example before anything ever gets rendered onto a
client-facing document.

Reference example (from the KCM/Weichert-style PEAR layout Brian supplied):
    Estimated Market Value:        $525,000
    Estimated Remaining Mortgage:  $210,000
    -> Total Estimated Home Equity: $315,000   (525,000 - 210,000)
    -> Equity Growth Milestone:     60%         (315,000 / 525,000)
    -> Value Appreciation:          $185,000    (525,000 - 340,000 2018 purchase)
    -> Equity You Could Access:     $252,000    (315,000 * 0.80)
All four figures are checked against this example in verify_against_example()
below.

NOTE: this module used to also compute a "Move-Up Buyer Potential" figure
(a down payment % against an assumed next-home target price, plus a PMI-
elimination flag). Brian's call (Cowork conversation, Nov 2026): that
framing assumes the client wants to buy a bigger primary residence, which
doesn't fit every recipient -- a downsizer, an investor, or an absentee
owner gets the same pitch regardless of their actual situation. Section 3
of the report was repositioned around the equity figure itself (what it
is, what it could be used for) rather than one assumed next transaction,
so suggested_target_price(), the move-up-% calc, and the PMI-threshold
check were all removed along with it. down_payment_power (now labeled
"Equity You Could Access" on the report) is the one figure from that old
section that's still genuinely useful on its own, so it stayed.
"""

DOWN_PAYMENT_POWER_PCT = 0.80  # "access up to 80% of your current equity"


def round_nearest(value, nearest=25000):
    if value is None:
        return None
    return int(round(value / nearest) * nearest)


def compute_pear(
    value,
    loan_balance,
    last_purchase_price=None,
    last_purchase_date=None,
):
    """All inputs are plain numbers (or None). Returns a dict of computed
    figures plus formatted display strings the template can drop straight
    in. Every computed field degrades to None when a required input is
    missing, rather than raising -- e.g. a client who owns their home
    free and clear (loan_balance = 0) is a perfectly valid, common case,
    while a *missing* value (None) for either figure means the section
    simply can't be computed yet and the review form should prompt the
    agent to fill it in."""
    out = {}

    value = value or None
    loan_balance = loan_balance if loan_balance is not None else None

    if value is not None and loan_balance is not None:
        out["total_equity"] = value - loan_balance
        out["equity_percent"] = round(out["total_equity"] / value * 100) if value else None
    else:
        out["total_equity"] = None
        out["equity_percent"] = None

    if value is not None and last_purchase_price:
        out["appreciation"] = value - last_purchase_price
    else:
        out["appreciation"] = None

    if out["total_equity"] is not None:
        out["down_payment_power"] = round(out["total_equity"] * DOWN_PAYMENT_POWER_PCT)
    else:
        out["down_payment_power"] = None

    return out


def verify_against_example():
    """Sanity check run at import time in debug/test contexts -- not
    called during normal request handling. Raises AssertionError with a
    clear message if the math ever drifts from Brian's worked example."""
    r = compute_pear(value=525000, loan_balance=210000, last_purchase_price=340000)
    checks = [
        ("total_equity", 315000),
        ("equity_percent", 60),
        ("appreciation", 185000),
        ("down_payment_power", 252000),
    ]
    for key, expected in checks:
        actual = r[key]
        assert actual == expected, f"{key}: expected {expected}, got {actual}"
    return True


if __name__ == "__main__":
    verify_against_example()
    print("All PEAR math checks passed against the reference example.")
