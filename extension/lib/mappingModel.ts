/**
 * The mapping-confirmation screen's state, without any DOM (Phase D session 4,
 * chrome-extension-design.md SS9 item 4). One role per sheet column, plus the entity name,
 * the scale the sheet's numbers were typed in, and an optional currency label.
 *
 * `validateDraft` mirrors the backend's validate_mapping rules (src/analysis/csv_statement.py)
 * so problems show up while editing. The backend stays the authority: its `errors[]` are
 * shown as it words them.
 *
 * Scale has no default, on purpose: a sheet typed in thousands and silently confirmed as
 * "ones" makes every answer wrong by 1000x. Confirm stays disabled until a scale is picked.
 * Currency defaults to "Not specified" (null), never a guessed USD.
 */

export const PERIOD_ROLE = 'period_end';
export const UNMAPPED_ROLE = 'unmapped';

export interface RoleOption {
  value: string;
  label: string;
}

/** The 13 concepts in the backend's CONCEPTS order, with plain labels. */
const CONCEPT_OPTIONS: RoleOption[] = [
  { value: 'revenue', label: 'Revenue' },
  { value: 'gross_profit', label: 'Gross profit' },
  { value: 'operating_income', label: 'Operating income' },
  { value: 'net_income', label: 'Net income' },
  { value: 'operating_cash_flow', label: 'Operating cash flow' },
  { value: 'capex', label: 'Capital expenditure' },
  { value: 'total_assets', label: 'Total assets' },
  { value: 'total_liabilities', label: 'Total liabilities' },
  { value: 'cash', label: 'Cash' },
  { value: 'stockholders_equity', label: "Stockholders' equity" },
  { value: 'current_assets', label: 'Current assets' },
  { value: 'current_liabilities', label: 'Current liabilities' },
  { value: 'liabilities_noncurrent', label: 'Non-current liabilities' },
];

export const ROLE_OPTIONS: RoleOption[] = [
  { value: UNMAPPED_ROLE, label: 'Not used' },
  { value: PERIOD_ROLE, label: 'Period end (date)' },
  ...CONCEPT_OPTIONS,
];

const VALID_ROLES = new Set(ROLE_OPTIONS.map((o) => o.value));

export function roleLabel(role: string): string {
  return ROLE_OPTIONS.find((o) => o.value === role)?.label ?? role;
}

export type Scale = 'ones' | 'thousands' | 'millions' | 'billions';

export const SCALE_OPTIONS: { value: Scale; label: string }[] = [
  { value: 'ones', label: 'Ones (as shown)' },
  { value: 'thousands', label: 'Thousands' },
  { value: 'millions', label: 'Millions' },
  { value: 'billions', label: 'Billions' },
];

/** null = "Not specified", the default. */
export const CURRENCY_OPTIONS: { value: string | null; label: string }[] = [
  { value: null, label: 'Not specified' },
  { value: 'USD', label: 'USD' },
  { value: 'EUR', label: 'EUR' },
  { value: 'GBP', label: 'GBP' },
  { value: 'CAD', label: 'CAD' },
  { value: 'AUD', label: 'AUD' },
  { value: 'JPY', label: 'JPY' },
];

export const ENTITY_NAME_MAX = 200;

export interface ProposalEntry {
  csv_column: string;
  proposed_role: string;
  rationale: string;
}

export interface MappingDraft {
  columns: string[];
  roles: Record<string, string>;
  rationales: Record<string, string>;
  entityName: string;
  scale: Scale | null;
  currency: string | null;
}

/** Each column's role from a proposal; a column the proposal doesn't name is "Not used". */
export function rolesFromProposal(columns: string[], proposal: ProposalEntry[] | null): Record<string, string> {
  const byColumn = new Map((proposal ?? []).map((p) => [p.csv_column, p.proposed_role]));
  return Object.fromEntries(
    columns.map((c) => {
      const role = byColumn.get(c);
      return [c, role && VALID_ROLES.has(role) ? role : UNMAPPED_ROLE];
    }),
  );
}

export function newDraft(
  columns: string[],
  proposal: ProposalEntry[] | null,
  entityName: string,
): MappingDraft {
  return {
    columns,
    roles: rolesFromProposal(columns, proposal),
    rationales: Object.fromEntries((proposal ?? []).map((p) => [p.csv_column, p.rationale])),
    entityName: entityName.slice(0, ENTITY_NAME_MAX),
    scale: null,
    currency: null,
  };
}

/**
 * "Change mapping": start from the previously confirmed mapping, matched by header name.
 * Headers that weren't in it keep the new proposal's role. Scale, currency and entity name
 * carry over too: they describe the same sheet.
 */
export function carryOver(
  draft: MappingDraft,
  previous: { roles: Record<string, string>; entityName: string; scale: Scale; currency: string | null },
): MappingDraft {
  const roles = { ...draft.roles };
  for (const column of draft.columns) {
    const role = previous.roles[column];
    if (role !== undefined && VALID_ROLES.has(role)) roles[column] = role;
  }
  return {
    ...draft,
    roles,
    entityName: previous.entityName,
    scale: previous.scale,
    currency: previous.currency,
  };
}

export interface DraftValidation {
  /** Problems that aren't about one column (missing period/revenue, name, scale). */
  formErrors: string[];
  /** A message per column involved in a conflict. */
  columnErrors: Record<string, string>;
  canConfirm: boolean;
}

export function validateDraft(draft: MappingDraft): DraftValidation {
  const formErrors: string[] = [];
  const columnErrors: Record<string, string> = {};
  const byRole = new Map<string, string[]>();
  for (const column of draft.columns) {
    const role = draft.roles[column] ?? UNMAPPED_ROLE;
    if (role === UNMAPPED_ROLE) continue;
    byRole.set(role, [...(byRole.get(role) ?? []), column]);
  }

  if (!byRole.has(PERIOD_ROLE)) formErrors.push('Choose the column that holds each row’s period end date.');
  if (!byRole.has('revenue')) formErrors.push('Choose the revenue column. Revenue is required.');
  for (const [role, columns] of byRole) {
    if (columns.length < 2) continue;
    for (const column of columns) {
      columnErrors[column] = `${roleLabel(role)} is also chosen for ${columns
        .filter((c) => c !== column)
        .map((c) => `“${c}”`)
        .join(', ')}. Pick one column.`;
    }
  }

  const name = draft.entityName.trim();
  if (name === '') formErrors.push('Enter the business name.');
  else if (name.length > ENTITY_NAME_MAX) formErrors.push(`The business name can be at most ${ENTITY_NAME_MAX} characters.`);
  if (draft.scale === null) formErrors.push('Choose the units the numbers are in.');

  return {
    formErrors,
    columnErrors,
    canConfirm: formErrors.length === 0 && Object.keys(columnErrors).length === 0,
  };
}

export interface ConfirmRequestBody {
  mapping: Record<string, string>;
  entity_name: string;
  scale: Scale;
  currency: string | null;
  accept_unparsed_cells: boolean;
  ack_fingerprint: string | null;
}

/**
 * The /confirm body. Scale is always sent explicitly; calling this with no scale chosen is a
 * bug in the caller (Confirm is disabled until one is), so it throws rather than default.
 */
export function buildConfirmRequest(
  draft: MappingDraft,
  acknowledgement: { fingerprint: string } | null = null,
): ConfirmRequestBody {
  if (draft.scale === null) throw new Error('buildConfirmRequest: no scale chosen');
  return {
    mapping: Object.fromEntries(draft.columns.map((c) => [c, draft.roles[c] ?? UNMAPPED_ROLE])),
    entity_name: draft.entityName.trim(),
    scale: draft.scale,
    currency: draft.currency,
    accept_unparsed_cells: acknowledgement !== null,
    ack_fingerprint: acknowledgement?.fingerprint ?? null,
  };
}
