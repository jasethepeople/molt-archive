/** Plain-text table rendering. No colour, no dependencies, pipes to `less`. */

export interface Column {
  header: string;
  /** Truncate longer values to this many characters. */
  max?: number;
}

function width(value: string, max?: number): number {
  const length = [...value].length;
  return max ? Math.min(length, max) : length;
}

function clip(value: string, max?: number): string {
  const chars = [...value];
  if (max && chars.length > max) return `${chars.slice(0, Math.max(1, max - 1)).join('')}…`;
  return value;
}

export function table(columns: Column[], rows: string[][]): string {
  if (rows.length === 0) return '(none)';
  const cells = rows.map((row) => columns.map((column, index) => clip(row[index] ?? '', column.max)));
  const widths = columns.map((column, index) =>
    Math.max(column.header.length, ...cells.map((row) => width(row[index] ?? ''))),
  );
  const line = (values: string[]): string =>
    values
      .map((value, index) => value.padEnd(widths[index] ?? 0))
      .join('  ')
      .replace(/\s+$/, '');
  const out = [line(columns.map((c) => c.header)), line(widths.map((w) => '-'.repeat(w)))];
  for (const row of cells) out.push(line(row.map((value, index) => clip(value, columns[index]?.max))));
  return out.join('\n');
}

export function heading(text: string): string {
  return `\n${text}\n${'='.repeat(text.length)}`;
}

export function bullet(value: string, text: string, detail?: string): string {
  return `  ${value.padEnd(28)} ${text}${detail ? `  ${detail}` : ''}`;
}

/** `unknown` stays `unknown`; anything else is shown as-is. */
export function orUnknown(value: string | null | undefined): string {
  const trimmed = (value ?? '').trim();
  return trimmed === '' ? 'unknown' : trimmed;
}

export function orDash(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const trimmed = String(value).trim();
  return trimmed === '' ? '—' : trimmed;
}
