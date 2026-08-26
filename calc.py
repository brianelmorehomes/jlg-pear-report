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
    -> Max Down Payment Power:      $252,000    (315,000 * 0.80)
    -> Move-Up Buyer (on a $700,000 target):
           down payment %  = 252,000 / 700,000 = 36%
           PMI eliminated  = 36% >= 20%  -> True
All five figures are checked against this example in verify_against_example()
below.
"""

DOWN_PAYMENT_POWER_PCT = 0.80  # "access up to 80% of your current equity"
PMI_ELIMINATION_THRESHOLD_PCT = 20.0  # conventional-loan rule of thumb


def round_nearest(value, nearest=25000):
    if value is None:
        return None
    return int(round(value / nearest) * nearest)


def suggested_target_price(current_value):
    """Default 'next home' price used to illustrate Move-Up Buyer Potential
    on the review form, before the agent overrides it with something that
    actually matches what this specific client is looking at. There's no
    formula tying this to the client's real plans -- it's just current
    value + 25%, rounded to a clean number, so the report doesn't render
    with a blank/zero target before the agent has a chance to edit it."""
    if not current_value:
        return None
    return round_nearest(current_value * 1.25, 25000)


def compute_pear(
    value,
    loan_balance,
    last_purchase_price=None,
    last_purchase_date=None,
    target_price=None,
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

    effective_target = target_price or suggested_target_price(value)
    out["target_price"] = effective_target
    if out["down_payment_power"] is not None and effective_target:
        out["move_up_down_payment_pct"] = round(out["down_payment_power"] / effective_target * 100)
        out["pmi_eliminated"] = out["move_up_down_payment_pct"] >= PMI_ELIMINATION_THRESHOLD_PCT
    else:
        out["move_up_down_payment_pct"] = None
        out["pmi_eliminated"] = None

    return out


def verify_against_example():
    """Sanity check run at import time in debug/test contexts -- not
    called during normal request handling. Raises AssertionError with a
    clear message if the math ever drifts from Brian's worked example."""
    r = compute_pear(value=525000, loan_balance=210000, last_purchase_price=340000, target_price=700000)
    checks = [
        ("total_equity", 315000),
        ("equity_percent", 60),
        ("appreciation", 185000),
        ("down_payment_power", 252000),
        ("move_up_down_payment_pct", 36),
        ("pmi_eliminated", True),
    ]
    for key, expected in checks:
        actual = r[key]
        assert actual == expected, f"{key}: expected {expected}, got {actual}"
    return True


if __name__ == "__main__":
    verify_against_example()
    print("All PEAR math checks passed against the reference example.")
