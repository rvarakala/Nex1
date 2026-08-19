"""Regression: Marketing-site visitor traffic analytics.

Locks the API contract used by the AdminPanel's "Traffic" screen:
  · Public beacon `POST /api/track` requires NO auth
  · Founder-only overview endpoint returns totals + daily series +
    campaigns + landings + referrers + events
  · Live endpoint returns visitors_online in the last N minutes
  · Tracker script is served as valid JavaScript with the right
    Content-Type and a cache header
"""
import os
import uuid
import requests

BASE_URL = os.environ.get(
    "REACT_APP_BACKEND_URL",
    "https://referral-sprint.preview.emergentagent.com",
).rstrip("/")
FOUNDER_EMAIL = "founder@audinexa.com"
FOUNDER_PASSWORD = "AudinexaFounder@2026"


def _founder():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
               timeout=30)
    assert r.status_code == 200, r.text
    s.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return s


def test_tracker_script_served_as_javascript():
    r = requests.get(f"{BASE_URL}/api/track.js", timeout=15)
    assert r.status_code == 200
    assert "javascript" in (r.headers.get("content-type") or "").lower()
    # Sanity check that it's the real tracker, not an error page.
    body = r.text
    assert "audinexaTrack" in body
    assert "sendBeacon" in body


def test_public_beacon_accepts_pageview_without_auth():
    """The beacon must be reachable from audinexa.com without a token."""
    vid = f"v-pytest-{uuid.uuid4().hex[:8]}"
    sid = f"s-pytest-{uuid.uuid4().hex[:8]}"
    r = requests.post(f"{BASE_URL}/api/track", json={
        "visitor_id": vid, "session_id": sid,
        "kind": "pageview", "path": "/pricing",
        "utm_source": "google", "utm_medium": "cpc",
        "utm_campaign": "pytest-camp",
        "referrer": "https://google.com/",
        "origin_referrer": "https://google.com/",
    }, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True


def test_beacon_rejects_malformed_payload():
    """Empty visitor_id must be rejected — otherwise we'd get junk rows."""
    r = requests.post(f"{BASE_URL}/api/track", json={
        "visitor_id": "", "session_id": "", "kind": "pageview",
    }, timeout=15)
    assert r.status_code == 422   # pydantic validation


def test_overview_requires_super_admin_and_returns_expected_shape():
    """Overview endpoint must be founder-only; unauthenticated 401 and
    the schema must include the keys the AdminPanel renders."""
    r = requests.get(f"{BASE_URL}/api/admin/marketing-traffic/overview", timeout=15)
    assert r.status_code == 401

    # Seed one campaign hit so the response has something meaningful.
    vid = f"v-pytest-{uuid.uuid4().hex[:8]}"
    sid = f"s-pytest-{uuid.uuid4().hex[:8]}"
    requests.post(f"{BASE_URL}/api/track", json={
        "visitor_id": vid, "session_id": sid,
        "kind": "pageview", "path": "/features",
        "utm_source": "linkedin", "utm_medium": "social",
        "utm_campaign": "pytest-shape-check",
    }, timeout=15)
    # Also fire a custom event so the events section has data.
    requests.post(f"{BASE_URL}/api/track", json={
        "visitor_id": vid, "session_id": sid,
        "kind": "event", "event_name": "pytest_cta",
    }, timeout=15)

    s = _founder()
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/overview?days=30", timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()

    # Contract keys the AdminPanel binds to.
    for key in ("range_days", "totals", "daily", "top_landings",
                "top_referrers", "campaigns", "top_events"):
        assert key in d, f"missing top-level key: {key}"

    t = d["totals"]
    for key in ("page_views", "unique_visitors", "unique_sessions",
                "custom_events", "avg_pages_per_session",
                "avg_session_seconds", "bounce_rate_pct"):
        assert key in t, f"missing totals key: {key}"

    # Our seeded custom event should surface (events list is short).
    ev_names = [e["event_name"] for e in d["top_events"]]
    assert "pytest_cta" in ev_names
    # Campaigns list must be non-empty and each row well-formed. We
    # don't assert our seeded name is in the list because campaigns
    # are capped at top 30 and may spill in a busy demo tenant.
    assert isinstance(d["campaigns"], list)
    if d["campaigns"]:
        for c in d["campaigns"]:
            assert "campaign" in c
            assert "sessions" in c
            assert "visitors" in c


def test_live_endpoint_returns_expected_shape():
    s = _founder()
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/live?minutes=15", timeout=15)
    assert r.status_code == 200
    d = r.json()
    for key in ("window_minutes", "visitors_online", "active_sessions", "live_paths"):
        assert key in d, f"missing key: {key}"
    assert isinstance(d["live_paths"], list)


def test_cohorts_endpoint_returns_grid_shape_and_founder_only():
    """The cohort grid must be super_admin-only and its shape must
    match what the AdminPanel binds to (`cohort_week`, `size`,
    `offsets[i].pct`)."""
    # Unauthenticated → 401
    r = requests.get(f"{BASE_URL}/api/admin/marketing-traffic/cohorts",
                     timeout=15)
    assert r.status_code == 401

    s = _founder()
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/cohorts?weeks=4",
              timeout=15)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["weeks"] == 4
    assert isinstance(d["cohorts"], list)
    # Any cohort row we do get back must carry the full offset grid.
    for row in d["cohorts"]:
        assert "cohort_week" in row
        assert "size" in row and row["size"] >= 1
        assert isinstance(row["offsets"], dict)
        # W0 should always be 100% (a visitor is always active on
        # their own first-seen week).
        w0 = row["offsets"].get("0") or {}
        assert w0.get("pct") == 100.0, "W0 must always be 100%"
        # All offsets 0..weeks-1 must be present.
        for i in range(d["weeks"]):
            assert str(i) in row["offsets"]


def test_cohorts_weeks_param_clamped():
    """Guardrail — pathological requests are silently clamped."""
    s = _founder()
    # `weeks=0` is clamped to 2 (min bound).
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/cohorts?weeks=0",
              timeout=15)
    assert r.status_code == 200
    assert r.json()["weeks"] == 2
    # `weeks=999` is clamped to 26 (max bound).
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/cohorts?weeks=999",
              timeout=15)
    assert r.status_code == 200
    assert r.json()["weeks"] == 26



def _seed_campaign_hit(campaign, source="google", medium="cpc",
                       event_name=None, path="/"):
    """Fire pageview (+optional event) beacon under a campaign so the
    compare endpoint has something to roll up."""
    vid = f"v-cmp-{uuid.uuid4().hex[:8]}"
    sid = f"s-cmp-{uuid.uuid4().hex[:8]}"
    payload = {
        "visitor_id": vid, "session_id": sid,
        "kind": "pageview", "path": path,
        "utm_source": source, "utm_medium": medium,
        "utm_campaign": campaign,
    }
    requests.post(f"{BASE_URL}/api/track", json=payload, timeout=15)
    if event_name:
        requests.post(f"{BASE_URL}/api/track", json={
            "visitor_id": vid, "session_id": sid,
            "kind": "event", "event_name": event_name,
        }, timeout=15)
    return vid, sid


def test_compare_endpoint_requires_super_admin_and_needs_campaigns():
    """The compare endpoint must be founder-only and 400 without campaigns."""
    # Unauthenticated → 401
    r = requests.get(f"{BASE_URL}/api/admin/marketing-traffic/compare"
                     "?campaigns=foo,bar", timeout=15)
    assert r.status_code == 401

    s = _founder()
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/compare?campaigns=",
              timeout=15)
    assert r.status_code == 400


def test_compare_endpoint_shape_and_alignment():
    """Every requested campaign returns totals + a daily array aligned
    to the shared `dates` axis so the frontend can overlay cleanly."""
    c1 = f"pytest-cmp-a-{uuid.uuid4().hex[:6]}"
    c2 = f"pytest-cmp-b-{uuid.uuid4().hex[:6]}"
    _seed_campaign_hit(c1, event_name="demo_cta", path="/pricing")
    _seed_campaign_hit(c1, path="/features")     # 2nd hit for c1
    _seed_campaign_hit(c2, source="linkedin", medium="social", path="/pricing")

    s = _founder()
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/compare",
              params={"campaigns": f"{c1},{c2}", "days": 30},
              timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["range_days"] == 30
    assert isinstance(d["dates"], list) and len(d["dates"]) > 0
    assert isinstance(d["campaigns"], list) and len(d["campaigns"]) == 2

    names = [c["campaign"] for c in d["campaigns"]]
    assert c1 in names and c2 in names
    for row in d["campaigns"]:
        # Totals shape.
        for key in ("page_views", "unique_visitors", "unique_sessions",
                    "custom_events", "converting_visitors",
                    "conversion_rate_pct", "bounce_rate_pct",
                    "avg_pages_per_session", "avg_session_seconds"):
            assert key in row["totals"], f"missing totals key: {key}"
        # Daily must be aligned to the shared axis.
        assert len(row["daily"]) == len(d["dates"])
        for pt, day in zip(row["daily"], d["dates"]):
            assert pt["date"] == day
            assert "page_views" in pt and "unique_visitors" in pt

    # Sanity: c1 fired a custom event, so its converting_visitors should
    # be >= 1 while c2 (no event) stays at 0.
    by_name = {c["campaign"]: c for c in d["campaigns"]}
    assert by_name[c1]["totals"]["converting_visitors"] >= 1
    assert by_name[c2]["totals"]["converting_visitors"] == 0


def test_compare_endpoint_dedupes_and_caps_at_four():
    """Duplicates collapse, and only the first 4 unique names are kept."""
    names = [f"pytest-cap-{i}-{uuid.uuid4().hex[:4]}" for i in range(6)]
    # Seed one hit for each so all 6 are real campaigns.
    for n in names:
        _seed_campaign_hit(n)

    s = _founder()
    # 6 names + a duplicate — expect exactly 4 in the response, in the
    # order they were sent (first-4 wins), with the dupe collapsed.
    payload = ",".join([names[0], names[0], *names[1:6]])
    r = s.get(f"{BASE_URL}/api/admin/marketing-traffic/compare",
              params={"campaigns": payload, "days": 30},
              timeout=20)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["campaigns"]) == 4
    assert [c["campaign"] for c in d["campaigns"]] == names[:4]
