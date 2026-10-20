import { BackendApiError, type UsageResponse } from '../../lib/backendApi';
import type { StoredSession } from '../../lib/sessionStorage';
import {
  billingErrorMessage,
  billingFingerprint,
  byoKeyErrorMessage,
  defaultFormatTime,
  isAllowedBillingUrl,
  UNEXPECTED_BILLING_URL_MESSAGE,
  usageView,
  type FormatTime,
} from '../../lib/usageModel';

/**
 * The usage panel (Phase D session 6): questions used and the limit, the tier, when more are
 * available; Upgrade and Manage subscription, which open Stripe's own hosted pages in a new
 * tab; and the person's own Anthropic key, added or removed.
 *
 * Card details are only ever entered on Stripe's pages: nothing here sees them. The tier
 * changes only when Stripe's webhook reaches the backend, so after sending the person to
 * Stripe the panel polls GET /v1/usage for a while once it's visible again, rather than
 * assuming anything changed (EXTENSION_INTEGRATION.md SS6).
 *
 * The Anthropic key exists only in the input's value and the one request that sends it. It's
 * never written to any storage, never logged, and the input is cleared whatever happens.
 */

export interface UsageElements {
  panel: HTMLElement;
  line: HTMLElement;
  resetLine: HTMLElement;
  notice: HTMLElement;
  upgradeButton: HTMLButtonElement;
  manageButton: HTMLButtonElement;
  refreshButton: HTMLButtonElement;
  keyForm: HTMLFormElement;
  keyInput: HTMLInputElement;
  saveKeyButton: HTMLButtonElement;
  removeKeyButton: HTMLButtonElement;
  keyNote: HTMLElement;
}

export interface UsageDeps {
  getSession(): Promise<StoredSession | null>;
  getUsage(sessionToken: string): Promise<UsageResponse>;
  createCheckoutSession(sessionToken: string): Promise<{ checkout_url: string }>;
  createPortalSession(sessionToken: string): Promise<{ portal_url: string }>;
  registerByoKey(sessionToken: string, apiKey: string): Promise<unknown>;
  removeByoKey(sessionToken: string): Promise<unknown>;
  /** Opens a Stripe page in a new tab (browser.tabs.create). */
  openTab(url: string): Promise<void>;
  /** A 401: end the session and show the signed-out UI. */
  onUnauthorized(): Promise<void>;
  isVisible(): boolean;
  now(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
  formatTime?: FormatTime;
}

/** After a trip to Stripe: check every 3 s for up to a minute, then stop quietly. */
export const STRIPE_POLL_INTERVAL_MS = 3_000;
export const STRIPE_POLL_ATTEMPTS = 20;

/**
 * After the portal only: a portal change's customer.subscription.updated webhook can arrive after
 * that minute (seen in session 6's production run), so keep checking quietly, every 15 s for
 * about 2 more minutes, while the panel stays visible.
 */
export const PORTAL_SLOW_POLL_INTERVAL_MS = 15_000;
export const PORTAL_SLOW_POLL_ATTEMPTS = 8;

type PollResult = 'changed' | 'unchanged' | 'stopped' | 'hidden';

export const WAITING_FOR_STRIPE_NOTE = 'Waiting for Stripe to confirm…';

/** The Refresh button's label while its own refresh is in flight. */
export const REFRESHING_LABEL = 'Updating…';

function isUnauthorized(err: unknown): boolean {
  return err instanceof BackendApiError && err.status === 401;
}

export class UsageController {
  private last: UsageResponse | null = null;
  private inFlight: Promise<UsageResponse | null> | null = null;
  /** The billing fingerprint before the person was sent to Stripe; null when not waiting. */
  private awaitingChangeFrom: string | null = null;
  /** Which Stripe page the person was sent to; only the portal gets the slow follow-up. */
  private awaitingKind: 'checkout' | 'portal' | null = null;
  private poll: AbortController | null = null;
  /** The slow portal follow-up: no waiting note, so the card shows its usual notice. */
  private quietPoll = false;
  private busy = false;
  /** A Refresh click is in flight: the button says so and can't be pressed again. */
  private refreshing = false;
  private readonly refreshLabel: string;

  constructor(
    private readonly el: UsageElements,
    private readonly deps: UsageDeps,
  ) {
    this.refreshLabel = el.refreshButton.textContent ?? '';
    el.upgradeButton.addEventListener('click', () => void this.openBilling('checkout'));
    el.manageButton.addEventListener('click', () => void this.openBilling('portal'));
    el.refreshButton.addEventListener('click', () => void this.refreshFromButton());
    el.removeKeyButton.addEventListener('click', () => void this.removeKey());
    el.keyForm.addEventListener('submit', (event) => {
      event.preventDefault();
      void this.saveKey();
    });
  }

  /** Signed out: hide the panel, forget everything, stop any poll. */
  reset(): void {
    this.poll?.abort();
    this.poll = null;
    this.quietPoll = false;
    this.awaitingChangeFrom = null;
    this.awaitingKind = null;
    this.last = null;
    this.el.keyInput.value = '';
    this.el.keyNote.textContent = '';
    this.el.notice.textContent = '';
    this.el.panel.hidden = true;
  }

  /** Fetches and renders usage. Concurrent calls share one request. */
  refresh(): Promise<UsageResponse | null> {
    if (!this.inFlight) {
      this.inFlight = this.load().finally(() => {
        this.inFlight = null;
      });
    }
    return this.inFlight;
  }

  /** Refresh, clicked: shows "Updating…" and disables the button until the request settles. */
  private async refreshFromButton(): Promise<void> {
    if (this.refreshing) return;
    this.setRefreshing(true);
    try {
      await this.refresh();
    } finally {
      this.setRefreshing(false);
    }
  }

  private setRefreshing(refreshing: boolean): void {
    this.refreshing = refreshing;
    this.el.refreshButton.textContent = refreshing ? REFRESHING_LABEL : this.refreshLabel;
    this.el.refreshButton.disabled = refreshing || this.busy;
  }

  /**
   * The panel became visible again (the person came back from a Stripe tab): refresh, and if
   * they were sent to Stripe, keep checking until the change arrives or a minute passes (about
   * three minutes after the portal; see PORTAL_SLOW_POLL_*).
   */
  onVisible(): void {
    if (!this.deps.isVisible()) return;
    if (this.awaitingChangeFrom !== null) void this.pollForStripe();
    else void this.refresh();
  }

  private async load(): Promise<UsageResponse | null> {
    const session = await this.deps.getSession();
    if (!session) return null;
    try {
      const usage = await this.deps.getUsage(session.sessionToken);
      this.last = usage;
      this.render(usage);
      return usage;
    } catch (err) {
      if (isUnauthorized(err)) {
        await this.deps.onUnauthorized();
        return null;
      }
      this.el.panel.hidden = false;
      this.el.line.textContent = "Couldn't load your usage. Select Refresh to try again.";
      return null;
    }
  }

  private render(usage: UsageResponse): void {
    const view = usageView(usage, new Date(this.deps.now()), this.deps.formatTime ?? defaultFormatTime);
    this.el.panel.hidden = false;
    this.el.line.textContent = view.line;
    this.el.resetLine.textContent = view.resetLine;
    if (this.poll === null || this.quietPoll) this.el.notice.textContent = view.notice;
    this.el.upgradeButton.hidden = !view.actions.upgrade;
    this.el.manageButton.hidden = !view.actions.manage;
    this.el.removeKeyButton.hidden = !view.actions.removeKey;
  }

  private setBusy(busy: boolean): void {
    this.busy = busy;
    for (const button of [
      this.el.upgradeButton,
      this.el.manageButton,
      this.el.refreshButton,
      this.el.saveKeyButton,
      this.el.removeKeyButton,
    ]) {
      button.disabled = busy;
    }
    if (this.refreshing) this.el.refreshButton.disabled = true;
  }

  private async openBilling(kind: 'checkout' | 'portal'): Promise<void> {
    if (this.busy) return;
    const session = await this.deps.getSession();
    if (!session) return;
    this.setBusy(true);
    try {
      const url =
        kind === 'checkout'
          ? (await this.deps.createCheckoutSession(session.sessionToken)).checkout_url
          : (await this.deps.createPortalSession(session.sessionToken)).portal_url;
      if (!isAllowedBillingUrl(url, kind)) {
        this.el.notice.textContent = UNEXPECTED_BILLING_URL_MESSAGE;
        return;
      }
      this.awaitingChangeFrom = this.last ? billingFingerprint(this.last) : '';
      this.awaitingKind = kind;
      await this.deps.openTab(url);
      this.el.notice.textContent =
        kind === 'checkout'
          ? 'Finish checkout in the new tab. This panel updates once Stripe confirms.'
          : 'Make your changes in the new tab. This panel updates once Stripe confirms.';
    } catch (err) {
      if (isUnauthorized(err)) {
        await this.deps.onUnauthorized();
        return;
      }
      // The panel's view was out of date (e.g. a subscription started in another window):
      // refresh first, so the re-render doesn't wipe the message below.
      if (err instanceof BackendApiError && (err.status === 409 || err.status === 404)) await this.refresh();
      this.el.notice.textContent = billingErrorMessage(err);
    } finally {
      this.setBusy(false);
    }
  }

  private async pollForStripe(): Promise<void> {
    if (this.poll !== null) return;
    const poll = new AbortController();
    this.poll = poll;
    const before = this.awaitingChangeFrom;
    const kind = this.awaitingKind;
    // Hidden during the slow follow-up: stop, but keep waiting, so the next onVisible resumes.
    let paused = false;
    this.el.notice.textContent = WAITING_FOR_STRIPE_NOTE;
    try {
      const fast = await this.checkUntilChanged(poll, before, STRIPE_POLL_ATTEMPTS, STRIPE_POLL_INTERVAL_MS, false);
      if (fast !== 'unchanged' || kind !== 'portal') return;
      this.quietPoll = true;
      if (this.last) this.render(this.last);
      else this.el.notice.textContent = '';
      const slow = await this.checkUntilChanged(poll, before, PORTAL_SLOW_POLL_ATTEMPTS, PORTAL_SLOW_POLL_INTERVAL_MS, true);
      paused = slow === 'hidden';
    } catch {
      return; // aborted (signed out); reset() already cleared the panel
    } finally {
      if (this.poll === poll) {
        this.poll = null;
        this.quietPoll = false;
        if (!paused) {
          this.awaitingChangeFrom = null;
          this.awaitingKind = null;
        }
        if (this.last) this.render(this.last);
        else this.el.notice.textContent = '';
      }
    }
  }

  /**
   * Checks /v1/usage up to `attempts` times, `intervalMs` apart, until the billing fingerprint
   * moves from `before`. The slow follow-up waits before its first check (the fast phase just
   * checked) and stops when the panel is hidden.
   */
  private async checkUntilChanged(
    poll: AbortController,
    before: string | null,
    attempts: number,
    intervalMs: number,
    slow: boolean,
  ): Promise<PollResult> {
    for (let attempt = 0; attempt < attempts; attempt++) {
      if (slow || attempt > 0) await this.deps.sleep(intervalMs, poll.signal);
      if (poll.signal.aborted) return 'stopped';
      if (slow && !this.deps.isVisible()) return 'hidden';
      const usage = await this.refresh();
      if (poll.signal.aborted || usage === null) return 'stopped';
      if (billingFingerprint(usage) !== before) return 'changed';
    }
    return 'unchanged';
  }

  private async saveKey(): Promise<void> {
    const apiKey = this.el.keyInput.value;
    this.el.keyInput.value = ''; // never kept in the page any longer than the request
    if (this.busy) return;
    if (apiKey.trim() === '') {
      this.el.keyNote.textContent = 'Paste your Anthropic API key first.';
      return;
    }
    const session = await this.deps.getSession();
    if (!session) return;
    this.setBusy(true);
    this.el.keyNote.textContent = 'Checking the key with Anthropic…';
    try {
      await this.deps.registerByoKey(session.sessionToken, apiKey);
      this.el.keyNote.textContent = 'Key saved. Your questions now use your own Anthropic key.';
      void this.refresh();
    } catch (err) {
      if (isUnauthorized(err)) {
        await this.deps.onUnauthorized();
        return;
      }
      // The status only: a 422 for a malformed request can echo the submitted body.
      console.warn('[usage] saving the Anthropic key failed', err instanceof BackendApiError ? err.status : 'network');
      this.el.keyNote.textContent = byoKeyErrorMessage(err);
    } finally {
      this.el.keyInput.value = '';
      this.setBusy(false);
    }
  }

  private async removeKey(): Promise<void> {
    if (this.busy) return;
    const session = await this.deps.getSession();
    if (!session) return;
    this.setBusy(true);
    try {
      await this.deps.removeByoKey(session.sessionToken);
      this.el.keyNote.textContent = 'Key removed. Your questions use your plan again.';
      void this.refresh();
    } catch (err) {
      if (isUnauthorized(err)) {
        await this.deps.onUnauthorized();
        return;
      }
      this.el.keyNote.textContent = "Couldn't remove the key. Try again in a moment.";
    } finally {
      this.setBusy(false);
    }
  }
}
