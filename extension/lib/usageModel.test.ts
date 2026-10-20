import { describe, expect, it } from 'vitest';
import { BackendApiError, type UsageResponse } from './backendApi';
import {
  billingErrorMessage,
  billingFingerprint,
  byoKeyErrorMessage,
  defaultFormatTime,
  isAllowedBillingUrl,
  usageView,
} from './usageModel';

const NOW = new Date('2026-10-08T12:00:00Z');
const fmt = (iso: string) => `<${iso}>`;

function usage(overrides: Partial<UsageResponse> = {}): UsageResponse {
  return {
    account_id: 'a',
    tier: 'free',
    questions_today: 2,
    daily_cap: 5,
    questions_this_period: null,
    monthly_cap: null,
    period_ends_at: null,
    byo_key_required: false,
    resets_at: '2026-10-09T00:00:00Z',
    has_byo_key: false,
    subscription: null,
    ...overrides,
  };
}

function paid(overrides: Partial<UsageResponse> = {}, cancel = false): UsageResponse {
  return usage({
    tier: 'paid',
    questions_today: null,
    daily_cap: null,
    questions_this_period: 7,
    monthly_cap: 50,
    period_ends_at: '2026-11-01T00:00:00Z',
    resets_at: '2026-11-01T00:00:00Z',
    subscription: { status: 'active', cancel_at_period_end: cancel, current_period_end: '2026-11-01T00:00:00Z' },
    ...overrides,
  });
}

const backend = (status: number, detail: unknown) => new BackendApiError(`failed (${status})`, status, detail);

describe('usageView', () => {
  it('free: used of the daily cap, when more arrive, and Upgrade', () => {
    const view = usageView(usage(), NOW, fmt);
    expect(view.line).toBe('Free: 2 of 5 questions today');
    expect(view.resetLine).toBe('Resets daily at 00:00 UTC (<2026-10-09T00:00:00Z> your time).');
    expect(view.notice).toBe('');
    expect(view.actions).toEqual({ upgrade: true, manage: false, removeKey: false });
  });

  it('free with none used yet: the same reset line, not "more questions from"', () => {
    expect(usageView(usage({ questions_today: 0 }), NOW, fmt).resetLine).toBe(
      'Resets daily at 00:00 UTC (<2026-10-09T00:00:00Z> your time).',
    );
  });

  it('paid: used of the period cap, the renewal date, and Manage instead of Upgrade', () => {
    const view = usageView(paid(), NOW, fmt);
    expect(view.line).toBe('Paid: 7 of 50 questions this period');
    expect(view.resetLine).toBe('Renews <2026-11-01T00:00:00Z>.');
    expect(view.actions).toEqual({ upgrade: false, manage: true, removeKey: false });
  });

  it('paid with a scheduled cancellation shows the end date', () => {
    expect(usageView(paid({}, true), NOW, fmt).resetLine).toBe('Your subscription ends <2026-11-01T00:00:00Z>.');
  });

  it('paid past its period end says the renewal is pending, not a past date', () => {
    const view = usageView(paid({ period_ends_at: '2026-10-01T00:00:00Z' }), NOW, fmt);
    expect(view.resetLine).toBe('Renewal pending: waiting for Stripe to confirm.');
  });

  it.each(['past_due', 'unpaid'])('%s: free tier, a payment notice, Manage and no Upgrade', (status) => {
    const view = usageView(
      usage({ subscription: { status, cancel_at_period_end: false, current_period_end: '2026-11-01T00:00:00Z' } }),
      NOW,
      fmt,
    );
    expect(view.line).toBe('Free: 2 of 5 questions today');
    expect(view.notice).toBe('Payment problem: update your card in Manage subscription.');
    expect(view.actions).toEqual({ upgrade: false, manage: true, removeKey: false });
  });

  it('after cancellation: Upgrade again, nothing to manage', () => {
    const view = usageView(
      usage({ subscription: { status: 'canceled', cancel_at_period_end: false, current_period_end: '2026-10-01T00:00:00Z' } }),
      NOW,
      fmt,
    );
    expect(view.actions).toEqual({ upgrade: true, manage: false, removeKey: false });
  });

  it('BYO key: billed to the person, Remove key, no reset line', () => {
    const view = usageView(usage({ tier: 'byo_key', questions_today: null, daily_cap: null, resets_at: null, has_byo_key: true }), NOW, fmt);
    expect(view.line).toBe('Using your own Anthropic key: questions are billed to your Anthropic account.');
    expect(view.resetLine).toBe('');
    expect(view.notice).toBe('');
    expect(view.actions).toEqual({ upgrade: true, manage: false, removeKey: true });
  });

  it('BYO key with a live subscription says the subscription is still billing', () => {
    const view = usageView(paid({ tier: 'byo_key', has_byo_key: true }), NOW, fmt);
    expect(view.notice).toBe('Your $2/month subscription is still active. Use Manage subscription to cancel it.');
    expect(view.actions).toEqual({ upgrade: false, manage: true, removeKey: true });
  });
});

describe('defaultFormatTime', () => {
  it('shows the date and time to the minute, never seconds', () => {
    // 10:07:42 UTC: whatever the runner's locale and zone, the seconds (42) must not appear.
    const shown = defaultFormatTime('2026-10-09T10:07:42Z');
    expect(shown).not.toMatch(/:42\b/);
    expect(shown).toMatch(/\d{1,2}[:.]\d{2}/);
  });
});

describe('billingFingerprint', () => {
  it('changes when the webhook lands, not when only the count moves', () => {
    const before = billingFingerprint(usage());
    expect(billingFingerprint(usage({ questions_today: 3 }))).toBe(before);
    expect(billingFingerprint(paid())).not.toBe(before);
    expect(billingFingerprint(paid({}, true))).not.toBe(billingFingerprint(paid()));
  });
});

describe('isAllowedBillingUrl', () => {
  it('opens only Stripe-hosted checkout and portal pages', () => {
    expect(isAllowedBillingUrl('https://checkout.stripe.com/c/pay/cs_test_1', 'checkout')).toBe(true);
    expect(isAllowedBillingUrl('https://billing.stripe.com/p/session/test_1', 'portal')).toBe(true);
    expect(isAllowedBillingUrl('https://billing.stripe.com/p/session/test_1', 'checkout')).toBe(false);
    expect(isAllowedBillingUrl('https://checkout.stripe.com.evil.example/', 'checkout')).toBe(false);
    expect(isAllowedBillingUrl('http://checkout.stripe.com/c', 'checkout')).toBe(false);
    expect(isAllowedBillingUrl('javascript:alert(1)', 'checkout')).toBe(false);
    expect(isAllowedBillingUrl(undefined, 'portal')).toBe(false);
  });
});

describe('error messages', () => {
  it('billing errors branch on detail.error', () => {
    expect(billingErrorMessage(backend(409, { error: 'subscription_exists' }))).toMatch(/already have a subscription/);
    expect(billingErrorMessage(backend(404, { error: 'no_billing_account' }))).toMatch(/no subscription to manage/);
    expect(billingErrorMessage(backend(503, { error: 'billing_unavailable' }))).toMatch(/isn't available/);
    expect(billingErrorMessage(backend(502, { error: 'billing_provider_error' }))).toMatch(/Stripe couldn't be reached/);
    expect(billingErrorMessage(new TypeError('network'))).toMatch(/Couldn't open billing/);
  });

  it('BYO key errors never repeat the key, even when a 422 body echoes it', () => {
    const key = 'sk-ant-api03-secret-value';
    const messages = [
      byoKeyErrorMessage(backend(422, { error: 'byo_key_rejected' })),
      byoKeyErrorMessage(backend(502, { error: 'byo_key_check_failed' })),
      byoKeyErrorMessage(backend(422, "That doesn't look like a valid Anthropic API key.")),
      byoKeyErrorMessage(backend(422, [{ loc: ['body', 'api_key'], input: key }])),
      byoKeyErrorMessage(new TypeError('network')),
    ];
    expect(messages[0]).toMatch(/didn't accept that key/);
    expect(messages[1]).toMatch(/Nothing was saved/);
    expect(messages[2]).toMatch(/Keys start with sk-ant-/);
    for (const message of messages) expect(message).not.toContain(key);
  });
});

describe('byo_unavailable (session 6 live test)', () => {
  it('says saving a key is unavailable and that nothing was saved', () => {
    expect(byoKeyErrorMessage(backend(503, { error: 'byo_unavailable' }))).toBe(
      "Saving your own key isn't available right now. Nothing was saved.",
    );
  });
});
