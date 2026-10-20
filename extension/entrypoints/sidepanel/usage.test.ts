// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeBrowser } from 'wxt/testing/fake-browser';
import { BackendApiError, type UsageResponse } from '../../lib/backendApi';
import {
  PORTAL_SLOW_POLL_ATTEMPTS,
  PORTAL_SLOW_POLL_INTERVAL_MS,
  REFRESHING_LABEL,
  STRIPE_POLL_ATTEMPTS,
  UsageController,
  WAITING_FOR_STRIPE_NOTE,
  type UsageDeps,
} from './usage';

const PANEL = `
  <section id="usage-panel" hidden>
    <p id="usage-line"></p><p id="usage-reset"></p><p id="usage-notice"></p>
    <button id="usage-upgrade" type="button" hidden></button>
    <button id="usage-manage" type="button" hidden></button>
    <button id="usage-refresh" type="button">Refresh</button>
    <form id="byo-key-form"><input id="byo-key-input" type="password" />
      <button id="byo-key-save" type="submit"></button>
      <button id="byo-key-remove" type="button" hidden></button>
      <p id="byo-key-note"></p></form>
  </section>`;

const KEY = 'sk-ant-api03-' + 'k'.repeat(40);
const $ = <T extends HTMLElement>(selector: string) => document.querySelector<T>(selector)!;

function free(overrides: Partial<UsageResponse> = {}): UsageResponse {
  return {
    account_id: 'a', tier: 'free', questions_today: 1, daily_cap: 5, questions_this_period: null,
    monthly_cap: null, period_ends_at: null, byo_key_required: false, resets_at: '2026-10-09T00:00:00Z',
    has_byo_key: false, subscription: null, ...overrides,
  };
}

const PAID: UsageResponse = free({
  tier: 'paid', questions_today: null, daily_cap: null, questions_this_period: 0, monthly_cap: 50,
  period_ends_at: '2026-11-08T00:00:00Z', resets_at: '2026-11-08T00:00:00Z',
  subscription: { status: 'active', cancel_at_period_end: false, current_period_end: '2026-11-08T00:00:00Z' },
});

const backend = (status: number, detail: unknown) => new BackendApiError(`failed (${status})`, status, detail);

async function settle(): Promise<void> {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}

function makePanel(usageResponses: Array<UsageResponse | Error>, overrides: Partial<UsageDeps> = {}) {
  document.body.innerHTML = PANEL;
  const deps: UsageDeps = {
    getSession: async () => ({ sessionToken: 'tok', accountId: 'a', expiresAt: 'e', provider: 'google' }),
    getUsage: vi.fn(async () => {
      const next = usageResponses.length > 1 ? usageResponses.shift()! : usageResponses[0]!;
      if (next instanceof Error) throw next;
      return next;
    }),
    createCheckoutSession: vi.fn(async () => ({ checkout_url: 'https://checkout.stripe.com/c/pay/cs_test_1' })),
    createPortalSession: vi.fn(async () => ({ portal_url: 'https://billing.stripe.com/p/session/test_1' })),
    registerByoKey: vi.fn(async () => ({ registered: true })),
    removeByoKey: vi.fn(async () => ({ removed: true })),
    openTab: vi.fn(async () => {}),
    onUnauthorized: vi.fn(async () => {}),
    isVisible: () => true,
    now: () => Date.parse('2026-10-08T12:00:00Z'),
    sleep: vi.fn(async () => {}),
    formatTime: (iso) => `<${iso}>`,
    ...overrides,
  };
  const panel = new UsageController(
    {
      panel: $('#usage-panel'), line: $('#usage-line'), resetLine: $('#usage-reset'), notice: $('#usage-notice'),
      upgradeButton: $('#usage-upgrade'), manageButton: $('#usage-manage'), refreshButton: $('#usage-refresh'),
      keyForm: $('#byo-key-form'), keyInput: $('#byo-key-input'), saveKeyButton: $('#byo-key-save'),
      removeKeyButton: $('#byo-key-remove'), keyNote: $('#byo-key-note'),
    },
    deps,
  );
  return { panel, deps };
}

function submitKey(value: string): void {
  $<HTMLInputElement>('#byo-key-input').value = value;
  $<HTMLFormElement>('#byo-key-form').dispatchEvent(new Event('submit', { cancelable: true }));
}

beforeEach(() => {
  fakeBrowser.reset();
});

describe('showing usage', () => {
  it('shows the tier, used and limit, the reset time, and the right buttons', async () => {
    const { panel } = makePanel([free()]);
    await panel.refresh();
    expect($('#usage-panel').hidden).toBe(false);
    expect($('#usage-line').textContent).toBe('Free: 1 of 5 questions today');
    expect($('#usage-reset').textContent).toBe('Resets daily at 00:00 UTC (<2026-10-09T00:00:00Z> your time).');
    expect($('#usage-upgrade').hidden).toBe(false);
    expect($('#usage-manage').hidden).toBe(true);
    expect($('#byo-key-remove').hidden).toBe(true);
  });

  it('a 401 ends the session', async () => {
    const { panel, deps } = makePanel([backend(401, 'Invalid or expired session token.')]);
    await panel.refresh();
    expect(deps.onUnauthorized).toHaveBeenCalledOnce();
  });

  it('other failures say so without hiding the panel', async () => {
    const { panel } = makePanel([backend(500, 'boom')]);
    await panel.refresh();
    expect($('#usage-panel').hidden).toBe(false);
    expect($('#usage-line').textContent).toMatch(/Couldn't load your usage/);
  });

  it('concurrent refreshes share one request', async () => {
    const { panel, deps } = makePanel([free()]);
    await Promise.all([panel.refresh(), panel.refresh(), panel.refresh()]);
    expect(deps.getUsage).toHaveBeenCalledOnce();
  });

  it('reset hides the panel and clears the key field', async () => {
    const { panel } = makePanel([free()]);
    await panel.refresh();
    $<HTMLInputElement>('#byo-key-input').value = KEY;
    panel.reset();
    expect($('#usage-panel').hidden).toBe(true);
    expect($<HTMLInputElement>('#byo-key-input').value).toBe('');
  });
});

/** A promise the test settles by hand, to look at the panel while a request is in flight. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('the Refresh button', () => {
  it('says "Updating…" and is disabled while its refresh runs, then restores', async () => {
    const pending = deferred<UsageResponse>();
    const { deps } = makePanel([free()], { getUsage: vi.fn(() => pending.promise) });
    const button = $<HTMLButtonElement>('#usage-refresh');
    button.click();
    await settle();
    expect(button.textContent).toBe(REFRESHING_LABEL);
    expect(button.disabled).toBe(true);

    button.click(); // ignored while in flight
    pending.resolve(free());
    await settle();
    expect(deps.getUsage).toHaveBeenCalledOnce();
    expect(button.textContent).toBe('Refresh');
    expect(button.disabled).toBe(false);
    expect($('#usage-line').textContent).toBe('Free: 1 of 5 questions today');
  });

  it('restores after a failed refresh, with the existing failure line', async () => {
    const pending = deferred<UsageResponse>();
    makePanel([free()], { getUsage: vi.fn(() => pending.promise) });
    const button = $<HTMLButtonElement>('#usage-refresh');
    button.click();
    await settle();
    pending.reject(backend(500, 'boom'));
    await settle();
    expect(button.textContent).toBe('Refresh');
    expect(button.disabled).toBe(false);
    expect($('#usage-line').textContent).toBe("Couldn't load your usage. Select Refresh to try again.");
  });

  it('stays disabled while a billing request is still running', async () => {
    const usage = deferred<UsageResponse>();
    const checkout = deferred<{ checkout_url: string }>();
    makePanel([free()], {
      getUsage: vi.fn(() => usage.promise),
      createCheckoutSession: vi.fn(() => checkout.promise),
    });
    const button = $<HTMLButtonElement>('#usage-refresh');
    button.click();
    await settle();
    $('#usage-upgrade').click();
    await settle();

    usage.resolve(free());
    await settle();
    expect(button.textContent).toBe('Refresh');
    expect(button.disabled).toBe(true); // Upgrade is still waiting on the backend

    checkout.resolve({ checkout_url: 'https://checkout.stripe.com/c/pay/cs_test_1' });
    await settle();
    expect(button.disabled).toBe(false);
  });
});

describe('Upgrade and Manage subscription', () => {
  it('Upgrade opens the Stripe checkout page in a new tab', async () => {
    const { panel, deps } = makePanel([free()]);
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();
    expect(deps.openTab).toHaveBeenCalledWith('https://checkout.stripe.com/c/pay/cs_test_1');
    expect($('#usage-notice').textContent).toMatch(/Finish checkout in the new tab/);
  });

  it('Manage opens the Stripe customer portal', async () => {
    const { panel, deps } = makePanel([PAID]);
    await panel.refresh();
    $('#usage-manage').click();
    await settle();
    expect(deps.openTab).toHaveBeenCalledWith('https://billing.stripe.com/p/session/test_1');
  });

  it('never opens an address that is not a Stripe page', async () => {
    const { panel, deps } = makePanel([free()], {
      createCheckoutSession: vi.fn(async () => ({ checkout_url: 'https://evil.example/checkout' })),
    });
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();
    expect(deps.openTab).not.toHaveBeenCalled();
    expect($('#usage-notice').textContent).toMatch(/unexpected address/);
  });

  it('a 409 subscription_exists explains and refreshes the stale view', async () => {
    const { panel, deps } = makePanel([free(), PAID], {
      createCheckoutSession: vi.fn(async () => { throw backend(409, { error: 'subscription_exists' }); }),
    });
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();
    expect($('#usage-notice').textContent).toMatch(/already have a subscription/);
    expect(deps.getUsage).toHaveBeenCalledTimes(2);
    expect($('#usage-manage').hidden).toBe(false);
  });

  it('on return, waits for Stripe and updates once the subscription appears', async () => {
    const { panel, deps } = makePanel([free(), free(), free(), PAID]);
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();

    panel.onVisible();
    expect($('#usage-notice').textContent).toBe(WAITING_FOR_STRIPE_NOTE);
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(4); // first load, then three checks
    expect($('#usage-line').textContent).toBe('Paid: 0 of 50 questions this period');
    expect($('#usage-notice').textContent).toBe('');
  });

  it('stops waiting after a minute if nothing changed (e.g. checkout abandoned)', async () => {
    const { panel, deps } = makePanel([free()]);
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();
    panel.onVisible();
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS);
    expect($('#usage-notice').textContent).toBe('');

    panel.onVisible(); // no longer waiting: a plain refresh
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(2 + STRIPE_POLL_ATTEMPTS);
  });

  it('after the portal, keeps checking quietly every 15 s and picks up a late cancellation', async () => {
    const CANCELED: UsageResponse = {
      ...PAID,
      subscription: { ...PAID.subscription!, cancel_at_period_end: true },
    };
    const notices: string[] = [];
    const responses = [PAID, ...Array<UsageResponse>(STRIPE_POLL_ATTEMPTS + 2).fill(PAID), CANCELED];
    const { panel, deps } = makePanel(responses, {
      sleep: vi.fn(async (ms: number) => {
        if (ms === PORTAL_SLOW_POLL_INTERVAL_MS) notices.push($('#usage-notice').textContent ?? '');
      }),
    });
    await panel.refresh();
    $('#usage-manage').click();
    await settle();
    panel.onVisible();
    await settle();
    await settle();

    // first load, the fast minute, then three slow checks
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS + 3);
    expect(deps.sleep).toHaveBeenCalledWith(PORTAL_SLOW_POLL_INTERVAL_MS, expect.anything());
    expect(notices.every((n) => n === '')).toBe(true); // no "Waiting for Stripe" in the slow phase
    expect($('#usage-reset').textContent).toBe('Your subscription ends <2026-11-08T00:00:00Z>.');
  });

  it('after the portal with no change, stops after the slow checks', async () => {
    const { panel, deps } = makePanel([PAID]);
    await panel.refresh();
    $('#usage-manage').click();
    await settle();
    panel.onVisible();
    await settle();
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS + PORTAL_SLOW_POLL_ATTEMPTS);
    expect($('#usage-notice').textContent).toBe('');

    panel.onVisible(); // no longer waiting: a plain refresh
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(2 + STRIPE_POLL_ATTEMPTS + PORTAL_SLOW_POLL_ATTEMPTS);
  });

  it('checkout gets no slow follow-up', async () => {
    const { panel, deps } = makePanel([free()]);
    await panel.refresh();
    $('#usage-upgrade').click();
    await settle();
    panel.onVisible();
    await settle();
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS);
    expect(deps.sleep).not.toHaveBeenCalledWith(PORTAL_SLOW_POLL_INTERVAL_MS, expect.anything());
  });

  it('a hidden panel pauses the slow follow-up, and showing it again resumes waiting', async () => {
    let visible = true;
    let slowSleeps = 0;
    const { panel, deps } = makePanel([PAID], {
      isVisible: () => visible,
      sleep: vi.fn(async (ms: number) => {
        if (ms === PORTAL_SLOW_POLL_INTERVAL_MS && ++slowSleeps === 2) visible = false;
      }),
    });
    await panel.refresh();
    $('#usage-manage').click();
    await settle();
    panel.onVisible();
    await settle();
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS + 1);

    visible = true;
    panel.onVisible(); // still waiting: polls again rather than a single refresh
    await settle();
    expect($('#usage-notice').textContent).toBe('');
    expect((deps.getUsage as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThan(2 + STRIPE_POLL_ATTEMPTS + 1);
  });

  it('signing out stops the slow follow-up', async () => {
    let slowSleeps = 0;
    const { panel, deps } = makePanel([PAID], {
      sleep: vi.fn(async (ms: number, signal: AbortSignal) => {
        if (ms === PORTAL_SLOW_POLL_INTERVAL_MS && ++slowSleeps === 1) panel.reset();
        if (signal.aborted) throw new DOMException('aborted', 'AbortError');
      }),
    });
    await panel.refresh();
    $('#usage-manage').click();
    await settle();
    panel.onVisible();
    await settle();
    await settle();
    expect(deps.getUsage).toHaveBeenCalledTimes(1 + STRIPE_POLL_ATTEMPTS);
    expect($('#usage-panel').hidden).toBe(true);
  });

  it('does nothing while the panel is hidden', async () => {
    const { panel, deps } = makePanel([free()], { isVisible: () => false });
    panel.onVisible();
    await settle();
    expect(deps.getUsage).not.toHaveBeenCalled();
  });
});

describe('your own Anthropic key', () => {
  it('saves the key, clears the field, and refreshes', async () => {
    const { panel, deps } = makePanel([free(), free({ tier: 'byo_key', has_byo_key: true })]);
    await panel.refresh();
    submitKey(KEY);
    await settle();
    expect(deps.registerByoKey).toHaveBeenCalledWith('tok', KEY);
    expect($<HTMLInputElement>('#byo-key-input').value).toBe('');
    expect($('#byo-key-note').textContent).toMatch(/Key saved/);
    expect($('#byo-key-remove').hidden).toBe(false);
  });

  it('a rejected key: a plain message, the field cleared, and the key never logged or stored', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { deps } = makePanel([free()], {
      // A FastAPI 422 for a malformed body can echo the submitted input back.
      registerByoKey: vi.fn(async () => { throw backend(422, [{ input: { api_key: KEY } }]); }),
    });
    submitKey(KEY);
    await settle();
    expect(deps.registerByoKey).toHaveBeenCalledOnce();
    expect($<HTMLInputElement>('#byo-key-input').value).toBe('');
    expect($('#byo-key-note').textContent).not.toContain(KEY);

    const logged = JSON.stringify([...warn.mock.calls, ...error.mock.calls]);
    expect(logged).not.toContain(KEY);
    expect(await fakeBrowser.storage.local.get()).toEqual({});
    expect(await fakeBrowser.storage.session.get()).toEqual({});
    expect(document.body.innerHTML).not.toContain(KEY);
  });

  it('Anthropic refusing the key gets its own message', async () => {
    makePanel([free()], {
      registerByoKey: vi.fn(async () => { throw backend(422, { error: 'byo_key_rejected' }); }),
    });
    submitKey(KEY);
    await settle();
    expect($('#byo-key-note').textContent).toMatch(/didn't accept that key/);
  });

  it('a blank field sends nothing', async () => {
    const { deps } = makePanel([free()]);
    submitKey('   ');
    await settle();
    expect(deps.registerByoKey).not.toHaveBeenCalled();
    expect($('#byo-key-note').textContent).toMatch(/Paste your Anthropic API key/);
  });

  it('Remove key removes it and refreshes', async () => {
    const { panel, deps } = makePanel([free({ tier: 'byo_key', has_byo_key: true }), free()]);
    await panel.refresh();
    $('#byo-key-remove').click();
    await settle();
    expect(deps.removeByoKey).toHaveBeenCalledWith('tok');
    expect($('#byo-key-note').textContent).toMatch(/Key removed/);
    expect($('#byo-key-remove').hidden).toBe(true);
  });

  it('a 401 while saving ends the session', async () => {
    const { deps } = makePanel([free()], {
      registerByoKey: vi.fn(async () => { throw backend(401, 'Invalid or expired session token.'); }),
    });
    submitKey(KEY);
    await settle();
    expect(deps.onUnauthorized).toHaveBeenCalledOnce();
    expect($<HTMLInputElement>('#byo-key-input').value).toBe('');
  });
});
