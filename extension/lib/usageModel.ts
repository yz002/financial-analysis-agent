import { BackendApiError, type UsageResponse } from './backendApi';

/**
 * What the usage panel shows (Phase D session 6, EXTENSION_INTEGRATION.md SS6 /v1/usage):
 * pure functions over GET /v1/usage's response, so every state's wording and buttons are
 * tested without a DOM. Times are formatted by the caller-supplied `formatTime` (the panel
 * passes the browser's locale formatting).
 */

/** Subscription states that still bill in Stripe: Upgrade is hidden while one exists. */
const LIVE_SUBSCRIPTION_STATUSES = new Set(['active', 'trialing', 'past_due', 'unpaid']);
const PAYMENT_PROBLEM_STATUSES = new Set(['past_due', 'unpaid']);
/** States with nothing left to manage in the portal. */
const ENDED_SUBSCRIPTION_STATUSES = new Set(['canceled', 'incomplete_expired']);

export type FormatTime = (iso: string) => string;

/** The browser's locale, to the minute: a reset or renewal time never needs seconds. */
export const defaultFormatTime: FormatTime = (iso) =>
  new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });

export interface UsageView {
  /** Tier and used/limit, e.g. "Free: 2 of 5 questions today". */
  line: string;
  /** When more questions are available, or when the subscription renews or ends. */
  resetLine: string;
  /** A billing state needing attention (payment problem, a subscription a BYO key overrides). */
  notice: string;
  actions: { upgrade: boolean; manage: boolean; removeKey: boolean };
}

export function usageView(usage: UsageResponse, now: Date, formatTime: FormatTime = defaultFormatTime): UsageView {
  const subscription = usage.subscription;
  const live = subscription !== null && LIVE_SUBSCRIPTION_STATUSES.has(subscription.status);

  let line: string;
  let resetLine = '';
  if (usage.tier === 'byo_key') {
    line = 'Using your own Anthropic key: questions are billed to your Anthropic account.';
  } else if (usage.tier === 'paid') {
    line = `Paid: ${usage.questions_this_period ?? 0} of ${usage.monthly_cap ?? 0} questions this period`;
    const periodEnd = usage.period_ends_at;
    if (subscription?.cancel_at_period_end && periodEnd) {
      resetLine = `Your subscription ends ${formatTime(periodEnd)}.`;
    } else if (periodEnd && new Date(periodEnd).getTime() <= now.getTime()) {
      resetLine = 'Renewal pending: waiting for Stripe to confirm.';
    } else if (periodEnd) {
      resetLine = `Renews ${formatTime(periodEnd)}.`;
    }
  } else {
    const used = usage.questions_today ?? 0;
    const cap = usage.daily_cap ?? 0;
    // Over the cap is legitimate: after a subscription ends, today's paid questions count toward
    // the free limit (EXTENSION_INTEGRATION.md SS6 /v1/usage). Shown as the backend's two numbers,
    // never clamped, but not as "6 of 5".
    line =
      used > cap
        ? `Free: ${used} question${used === 1 ? '' : 's'} counted today (daily limit ${cap})`
        : `Free: ${used} of ${cap} questions today`;
    // The free limit resets at 00:00 UTC (EXTENSION_INTEGRATION.md SS6 /v1/usage), so say so
    // rather than show only a local time; the same line whether or not any are used yet.
    if (usage.resets_at) resetLine = `Resets daily at 00:00 UTC (${formatTime(usage.resets_at)} your time).`;
  }

  let notice = '';
  if (subscription && PAYMENT_PROBLEM_STATUSES.has(subscription.status)) {
    notice = 'Payment problem: update your card in Manage subscription.';
  } else if (usage.tier === 'byo_key' && live) {
    notice = 'Your $2/month subscription is still active. Use Manage subscription to cancel it.';
  }

  return {
    line,
    resetLine,
    notice,
    actions: {
      upgrade: !live,
      manage: subscription !== null && !ENDED_SUBSCRIPTION_STATUSES.has(subscription.status),
      removeKey: usage.has_byo_key,
    },
  };
}

/**
 * What a completed checkout or portal change looks like from /v1/usage. The panel polls after
 * sending the person to Stripe, and stops once this changes (the webhook has arrived).
 */
export function billingFingerprint(usage: UsageResponse): string {
  const s = usage.subscription;
  return JSON.stringify([usage.tier, usage.has_byo_key, s?.status ?? null, s?.cancel_at_period_end ?? null, s?.current_period_end ?? null]);
}

const BILLING_URL_PREFIX = {
  checkout: 'https://checkout.stripe.com/',
  portal: 'https://billing.stripe.com/',
} as const;

/** Only Stripe's own hosted pages are ever opened from a backend response. */
export function isAllowedBillingUrl(url: unknown, kind: keyof typeof BILLING_URL_PREFIX): url is string {
  return typeof url === 'string' && url.startsWith(BILLING_URL_PREFIX[kind]);
}

export const UNEXPECTED_BILLING_URL_MESSAGE =
  "Billing returned an unexpected address, so it wasn't opened. This is a bug; please report it.";

function errorCode(err: BackendApiError): string | null {
  const detail = err.detail;
  if (detail && typeof detail === 'object' && 'error' in detail) {
    const code = (detail as { error: unknown }).error;
    return typeof code === 'string' ? code : null;
  }
  return null;
}

/** For a failed Upgrade or Manage subscription click. Branches on detail.error, never on text. */
export function billingErrorMessage(err: unknown): string {
  if (err instanceof BackendApiError && err.fromBackend) {
    switch (errorCode(err)) {
      case 'subscription_exists':
        return 'You already have a subscription. Use Manage subscription to change it.';
      case 'no_billing_account':
        return "There's no subscription to manage yet.";
      case 'billing_unavailable':
        return "Billing isn't available right now.";
      case 'billing_provider_error':
        return "Stripe couldn't be reached. Try again in a moment.";
    }
  }
  return "Couldn't open billing. Try again in a moment.";
}

/** For a failed Save key. The key itself never appears in any message. */
export function byoKeyErrorMessage(err: unknown): string {
  if (err instanceof BackendApiError && err.fromBackend) {
    const code = errorCode(err);
    if (code === 'byo_key_rejected') return "Anthropic didn't accept that key. Check it and try again.";
    if (code === 'byo_unavailable') return "Saving your own key isn't available right now. Nothing was saved.";
    if (code === 'byo_key_check_failed') {
      return "Couldn't check the key with Anthropic right now. Nothing was saved; try again in a moment.";
    }
    if (err.status === 422) return "That doesn't look like an Anthropic API key. Keys start with sk-ant-.";
  }
  return "Couldn't save the key. Nothing was saved; try again in a moment.";
}
