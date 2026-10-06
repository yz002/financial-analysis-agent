import { SCALE_OPTIONS } from './mappingModel';
import type { ActiveStatement } from './sessionStorage';

/**
 * How a confirmed statement is named in the panel (Phase D session 5). Two statements read from
 * the same range must be told apart -- for example the same rows confirmed once in thousands and
 * once in millions -- so the description carries the scale and when it was confirmed. The
 * business name isn't repeated when the label already starts with it (it defaults to the file
 * name), which is what produced "FA Spike Test — FA Spike Test · 'P&L'!A3:G7".
 */

/** The business name and where the rows came from, without repeating the name. */
export function statementSource(statement: ActiveStatement): string {
  const { entityName, label } = statement;
  if (label === entityName || label.startsWith(`${entityName} · `)) return label;
  return `${entityName} — ${label}`;
}

function scaleText(scale: string): string {
  const label = SCALE_OPTIONS.find((o) => o.value === scale)?.label ?? scale;
  return `numbers in ${label.toLowerCase()}`;
}

function confirmedText(confirmedAt: string | null | undefined): string | null {
  if (!confirmedAt) return null;
  const when = new Date(confirmedAt);
  if (Number.isNaN(when.getTime())) return null;
  return `confirmed ${when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}`;
}

/** e.g. "FA Spike Test · 'P&L'!A3:G7 · numbers in thousands · confirmed 5 Oct 2026, 14:03". */
export function describeStatement(statement: ActiveStatement): string {
  return [statementSource(statement), scaleText(statement.scale), confirmedText(statement.confirmedAt)]
    .filter((part): part is string => Boolean(part))
    .join(' · ');
}
