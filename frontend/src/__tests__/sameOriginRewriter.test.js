/**
 * Same-origin URL rewriter — production safety net.
 *
 * Run: `cd /app/frontend && yarn test --watchAll=false src/__tests__/sameOriginRewriter.test.js`
 *
 * Defends against the failure mode where `REACT_APP_BACKEND_URL` is baked
 * into the production bundle pointing at the WRONG host (e.g. preview backend
 * instead of audinexa.com). When the frontend at audinexa.com calls a
 * different host, cookies don't attach → every page shows "Not authenticated"
 * even though the backend is healthy. This rewriter fixes the call URL
 * silently at runtime so cookies always attach.
 */
import { rewriteToSameOriginIfNeeded } from '../auth/sameOriginRewriter';

describe('rewriteToSameOriginIfNeeded', () => {
  const RealLocation = window.location;
  afterEach(() => {
    // Restore window.location after each test.
    Object.defineProperty(window, 'location', { value: RealLocation, configurable: true });
  });
  const setHost = (origin) => {
    Object.defineProperty(window, 'location', {
      value: new URL(origin),
      configurable: true,
    });
  };

  test('rewrites cross-origin /api/* URL to same-origin (the production bug)', () => {
    setHost('https://audinexa.com');
    const stale = 'https://careful-feedback.emergent.host/api/admin/v2/dashboard';
    expect(rewriteToSameOriginIfNeeded(stale))
      .toBe('https://audinexa.com/api/admin/v2/dashboard');
  });

  test('leaves same-origin URL untouched', () => {
    setHost('https://audinexa.com');
    const same = 'https://audinexa.com/api/admin/v2/tenants';
    expect(rewriteToSameOriginIfNeeded(same)).toBe(same);
  });

  test('preserves query string when rewriting', () => {
    setHost('https://audinexa.com');
    const stale = 'https://careful-feedback.emergent.host/api/admin/v2/tenants?limit=5&q=test';
    expect(rewriteToSameOriginIfNeeded(stale))
      .toBe('https://audinexa.com/api/admin/v2/tenants?limit=5&q=test');
  });

  test('leaves non-API third-party URLs alone (Razorpay, fonts, analytics)', () => {
    setHost('https://audinexa.com');
    const razorpay = 'https://checkout.razorpay.com/v1/checkout.js';
    expect(rewriteToSameOriginIfNeeded(razorpay)).toBe(razorpay);
    const fonts = 'https://fonts.googleapis.com/css2?family=Inter';
    expect(rewriteToSameOriginIfNeeded(fonts)).toBe(fonts);
  });

  test('leaves relative URLs untouched (they are already same-origin)', () => {
    setHost('https://audinexa.com');
    expect(rewriteToSameOriginIfNeeded('/api/auth/me')).toBe('/api/auth/me');
  });

  test('preview environment: env var matches page host → no-op', () => {
    setHost('https://referral-sprint.preview.emergentagent.com');
    const same = 'https://referral-sprint.preview.emergentagent.com/api/auth/me';
    expect(rewriteToSameOriginIfNeeded(same)).toBe(same);
  });

  test('handles null / undefined / non-URL strings gracefully', () => {
    setHost('https://audinexa.com');
    expect(rewriteToSameOriginIfNeeded('')).toBe('');
    expect(rewriteToSameOriginIfNeeded(null)).toBe(null);
    expect(rewriteToSameOriginIfNeeded(undefined)).toBe(undefined);
    expect(rewriteToSameOriginIfNeeded('not-a-url')).toBe('not-a-url');
  });

  test('does not rewrite cross-origin URLs that are NOT /api/* calls', () => {
    setHost('https://audinexa.com');
    // /static/... on a foreign host — leave alone.
    const stale = 'https://careful-feedback.emergent.host/static/images/logo.png';
    expect(rewriteToSameOriginIfNeeded(stale)).toBe(stale);
  });
});
