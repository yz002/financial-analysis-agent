import { describe, expect, it } from 'vitest';
import {
  buildConfirmRequest,
  carryOver,
  newDraft,
  ROLE_OPTIONS,
  validateDraft,
  type MappingDraft,
  type ProposalEntry,
} from './mappingModel';

const COLUMNS = ['Quarter', 'Revenue', 'Net income', 'Notes'];
const PROPOSAL: ProposalEntry[] = [
  { csv_column: 'Quarter', proposed_role: 'period_end', rationale: 'dates' },
  { csv_column: 'Revenue', proposed_role: 'revenue', rationale: 'sales' },
  { csv_column: 'Net income', proposed_role: 'net_income', rationale: 'bottom line' },
];

function ready(overrides: Partial<MappingDraft> = {}): MappingDraft {
  return { ...newDraft(COLUMNS, PROPOSAL, 'FA Spike Test'), scale: 'ones', ...overrides };
}

describe('newDraft', () => {
  it('takes roles from the proposal, and "Not used" for anything it skipped or got wrong', () => {
    const draft = newDraft(
      [...COLUMNS, 'Odd'],
      [...PROPOSAL, { csv_column: 'Odd', proposed_role: 'made_up', rationale: '' }],
      'Co',
    );
    expect(draft.roles).toEqual({
      Quarter: 'period_end',
      Revenue: 'revenue',
      'Net income': 'net_income',
      Notes: 'unmapped',
      Odd: 'unmapped',
    });
    expect(draft.rationales.Revenue).toBe('sales');
  });

  it('starts with no scale and no currency: neither is ever guessed', () => {
    const draft = newDraft(COLUMNS, PROPOSAL, 'Co');
    expect(draft.scale).toBeNull();
    expect(draft.currency).toBeNull();
  });

  it('without a proposal every column is "Not used"', () => {
    expect(Object.values(newDraft(COLUMNS, null, 'Co').roles)).toEqual(Array(4).fill('unmapped'));
  });

  it('offers the 13 concepts, period end and Not used', () => {
    expect(ROLE_OPTIONS).toHaveLength(15);
  });
});

describe('validateDraft mirrors the backend rules', () => {
  it('a complete draft can be confirmed', () => {
    expect(validateDraft(ready())).toEqual({ formErrors: [], columnErrors: {}, canConfirm: true });
  });

  it('Confirm stays disabled until a scale is chosen', () => {
    const result = validateDraft(ready({ scale: null }));
    expect(result.canConfirm).toBe(false);
    expect(result.formErrors).toEqual(['Choose the units the numbers are in.']);
  });

  it('needs a period column and a revenue column', () => {
    const draft = ready();
    draft.roles = { ...draft.roles, Quarter: 'unmapped', Revenue: 'unmapped' };
    const result = validateDraft(draft);
    expect(result.canConfirm).toBe(false);
    expect(result.formErrors).toHaveLength(2);
  });

  it('flags a role on two columns on both rows', () => {
    const draft = ready();
    draft.roles = { ...draft.roles, 'Net income': 'revenue' };
    const result = validateDraft(draft);
    expect(result.canConfirm).toBe(false);
    expect(Object.keys(result.columnErrors).sort()).toEqual(['Net income', 'Revenue']);
    expect(result.columnErrors.Revenue).toContain('“Net income”');
  });

  it('needs a non-blank business name of at most 200 characters', () => {
    expect(validateDraft(ready({ entityName: '   ' })).canConfirm).toBe(false);
    expect(validateDraft(ready({ entityName: 'x'.repeat(201) })).canConfirm).toBe(false);
    expect(validateDraft(ready({ entityName: 'x'.repeat(200) })).canConfirm).toBe(true);
  });
});

describe('buildConfirmRequest', () => {
  it('sends every column, the chosen scale explicitly, and a trimmed name', () => {
    expect(buildConfirmRequest(ready({ entityName: '  Spike Co ', scale: 'thousands', currency: 'EUR' }))).toEqual({
      mapping: { Quarter: 'period_end', Revenue: 'revenue', 'Net income': 'net_income', Notes: 'unmapped' },
      entity_name: 'Spike Co',
      scale: 'thousands',
      currency: 'EUR',
      accept_unparsed_cells: false,
      ack_fingerprint: null,
    });
  });

  it('sends "ones" when the person chose it, and null currency when not specified', () => {
    const body = buildConfirmRequest(ready());
    expect(body.scale).toBe('ones');
    expect(body.currency).toBeNull();
  });

  it('an acknowledgement sends accept_unparsed_cells with its fingerprint', () => {
    const body = buildConfirmRequest(ready(), { fingerprint: 'abc' });
    expect(body.accept_unparsed_cells).toBe(true);
    expect(body.ack_fingerprint).toBe('abc');
  });

  it('refuses to build a request with no scale rather than default one', () => {
    expect(() => buildConfirmRequest(ready({ scale: null }))).toThrow('no scale');
  });
});

describe('carryOver (Change mapping)', () => {
  it('keeps the confirmed roles for matching headers, and the new proposal for the rest', () => {
    const fresh = newDraft([...COLUMNS, 'Cash'], [...PROPOSAL, { csv_column: 'Cash', proposed_role: 'cash', rationale: '' }], 'New');
    const draft = carryOver(fresh, {
      roles: { Quarter: 'period_end', Revenue: 'revenue', 'Net income': 'unmapped', Gone: 'capex' },
      entityName: 'Spike Co',
      scale: 'millions',
      currency: 'GBP',
    });
    expect(draft.roles).toEqual({
      Quarter: 'period_end',
      Revenue: 'revenue',
      'Net income': 'unmapped',
      Notes: 'unmapped',
      Cash: 'cash',
    });
    expect([draft.entityName, draft.scale, draft.currency]).toEqual(['Spike Co', 'millions', 'GBP']);
  });
});
