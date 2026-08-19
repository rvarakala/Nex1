"""API-level e2e tests for pair-quote side='both' expansion via public URL."""
import os
import requests
import pytest

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://referral-sprint.preview.emergentagent.com").rstrip("/")
EMAIL = "owner@thesoundclinic.in"
PWD = "demo123"
BRANCH_ID = "BR-SOUNDCLINIC-HQ"
PATIENT_ID = "ACS-2026-CFFCC3E8"
PRODUCT_ID = "PRD-3A545B49"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": EMAIL, "password": PWD}, timeout=30)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    tok = r.json().get("access_token") or r.json().get("token")
    s.headers.update({"Authorization": f"Bearer {tok}"})
    return s


def _base_payload(is_pair, lines):
    return {
        "branch_id": BRANCH_ID,
        "patient_id": PATIENT_ID,
        "is_pair": is_pair,
        "lines": lines,
    }


def test_pair_quote_with_side_both_expands(session):
    payload = _base_payload(True, [{
        "product_id": PRODUCT_ID, "side": "both", "qty": 1,
        "unit_price": 160000, "discount_pct": 30, "gst_rate": 0,
    }])
    r = session.post(f"{BASE}/api/ha/quotations", json=payload, timeout=30)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    sides = sorted([l.get("side") for l in body.get("lines", [])])
    assert sides == ["left", "right"], f"got {sides}"
    assert abs(float(body["total"]) - 224000) < 1, f"total={body['total']}"


def test_pair_quote_explicit_left_right(session):
    payload = _base_payload(True, [
        {"product_id": PRODUCT_ID, "side": "left", "qty": 1, "unit_price": 160000, "discount_pct": 30, "gst_rate": 0},
        {"product_id": PRODUCT_ID, "side": "right", "qty": 1, "unit_price": 160000, "discount_pct": 30, "gst_rate": 0},
    ])
    r = session.post(f"{BASE}/api/ha/quotations", json=payload, timeout=30)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert len(body["lines"]) == 2
    assert abs(float(body["total"]) - 224000) < 1


def test_non_pair_quote_unchanged(session):
    payload = _base_payload(False, [{
        "product_id": PRODUCT_ID, "side": "single", "qty": 1,
        "unit_price": 160000, "discount_pct": 30, "gst_rate": 0,
    }])
    r = session.post(f"{BASE}/api/ha/quotations", json=payload, timeout=30)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    body = r.json()
    assert len(body["lines"]) == 1
    assert body["lines"][0]["side"] == "single"


def test_error_message_no_serialised_word(session):
    payload = _base_payload(True, [])
    r = session.post(f"{BASE}/api/ha/quotations", json=payload, timeout=30)
    assert r.status_code >= 400
    detail = str(r.json()).lower()
    assert "serialised" not in detail, f"still says 'serialised': {detail}"


def test_get_quote_detail_has_two_lines(session):
    payload = _base_payload(True, [{
        "product_id": PRODUCT_ID, "side": "both", "qty": 1,
        "unit_price": 160000, "discount_pct": 30, "gst_rate": 0,
    }])
    r = session.post(f"{BASE}/api/ha/quotations", json=payload, timeout=30)
    assert r.status_code == 200
    quote_no = r.json()["quote_no"]
    r2 = session.get(f"{BASE}/api/ha/quotations/{quote_no}", timeout=30)
    assert r2.status_code == 200
    body = r2.json()
    sides = sorted([l["side"] for l in body["lines"]])
    assert sides == ["left", "right"]
    for l in body["lines"]:
        assert l["qty"] == 1
        assert l["unit_price"] == 160000
        assert l["discount_pct"] == 30
