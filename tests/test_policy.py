from decimal import Decimal

MGR = "manager@example.com"
DIR = "director@example.com"


def test_po_tiers_unchanged(policy):
    assert policy.required_tier(Decimal("50000")).name == "manager"
    assert policy.required_tier(Decimal("50001")).name == "director"
    assert policy.approver_limit(MGR) == Decimal("50000")


def test_invoice_limits(policy):
    assert policy.approver_limit("controller@example.com", "purchase_invoice") == Decimal("250000")
    assert policy.approver_limit(DIR, "purchase_invoice") == Decimal("250000")
    assert policy.approver_limit(MGR, "purchase_invoice") is None
    assert policy.required_tier(Decimal("250001"), "purchase_invoice").name == "cfo"


def test_payment_limits(policy):
    assert policy.approver_limit(MGR, "payment") == Decimal("50000")
    assert policy.approver_limit(DIR, "payment") == Decimal("250000")
    assert policy.approver_limit("treasury@example.com", "payment") is None


def test_purchase_request_uses_po_tiers(policy):
    assert policy.required_tier(Decimal("60000"), "purchase_request").name == "director"
    assert policy.approver_limit(MGR, "purchase_request") == Decimal("50000")


def test_requester_cannot_approve(policy):
    assert policy.approver_limit("requester@example.com") is None
    assert policy.approver_limit("nobody@example.com") is None


def test_new_sections(policy):
    assert policy.request_required_from == Decimal("5000")
    assert policy.quotes.threshold == Decimal("25000")
    assert policy.quotes.count == 3
    assert policy.quotes.lookback_days == 90
    assert policy.over_receipt_pct == Decimal("5")
    assert policy.invoice.price_tolerance_pct == Decimal("2")
    assert policy.invoice.dup_amount_days == 30
    assert policy.invoice.near_days == 14
    assert policy.invoice.near_pct == Decimal("0.5")
    assert policy.payment.dup_days == 30
    assert policy.payment.new_supplier_days == 90
    assert policy.payment.bank_account == "Demo Operating Bank - LDU"
    assert policy.second_person == set()


def test_roles(policy):
    assert "receiver" in policy.roles["receiver@example.com"]
    assert "ap" in policy.roles["ap@example.com"]
    assert "controller" in policy.roles["controller@example.com"]
    assert "treasury" in policy.roles["treasury@example.com"]
    assert "approver" in policy.roles[MGR]
