/**
 * A small, tolerant markdown block scanner.
 *
 * It does not try to be CommonMark. It recognises exactly the shapes a network
 * map is written in — headings, tables, bullets, paragraphs, fenced blocks — and
 * keeps the 1-based line number of everything, so the semantic layer can say
 * "map.md:118" for every record it produces and for everything it drops.
 */

export interface HeadingBlock {
  kind: 'heading';
  line: number;
  level: number;
  text: string;
}

export interface TableRow {
  cells: string[];
  /** URLs lifted from `[text](url)` in each cell, positionally. */
  links: string[][];
  line: number;
}

export interface TableBlock {
  kind: 'table';
  line: number;
  header: string[];
  rows: TableRow[];
}

export interface BulletBlock {
  kind: 'bullet';
  line: number;
  indent: number;
  text: string;
  /** URLs lifted from `[text](url)` in the bullet. */
  links: string[];
}

export interface ParaBlock {
  kind: 'para';
  line: number;
  text: string;
  links: string[];
}

export interface CodeBlock {
  kind: 'code';
  line: number;
  lang: string;
  body: string;
}

export type Block = HeadingBlock | TableBlock | BulletBlock | ParaBlock | CodeBlock;

const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$/;
const FENCE = /^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$/;
const TABLE_SEPARATOR = /^\s*\|?\s*:?-{1,}:?\s*(?:\|\s*:?-{1,}:?\s*)*\|?\s*$/;

/** Strip inline emphasis/code markers from a cell without touching `snake_case`. */
export function cleanInline(value: string): string {
  return value
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|[\s(])\*([^*]+)\*(?=[\s).,]|$)/g, '$1$2')
    .replace(/(^|[\s(])_([^_]+)_(?=[\s).,]|$)/g, '$1$2')
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
}

export function splitTableRow(line: string): string[] {
  const sentinel = '\u0000PIPE\u0000';
  const escaped = line.replace(/\\\|/g, sentinel);
  let body = escaped.trim();
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|')) body = body.slice(0, -1);
  return body.split('|').map((cell) => cleanInline(cell.split(sentinel).join('|')));
}

export function splitTableRowWithLinks(line: string): { cells: string[]; links: string[][] } {
  const sentinel = '\u0000PIPE\u0000';
  const escaped = line.replace(/\\\|/g, sentinel);
  let body = escaped.trim();
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|')) body = body.slice(0, -1);
  const rawCells = body.split('|').map((cell) => cell.split(sentinel).join('|'));
  return {
    cells: rawCells.map((cell) => cleanInline(cell)),
    links: rawCells.map((cell) => extractLinks(cell)),
  };
}

export interface ScanResult {
  blocks: Block[];
  lines: string[];
}

export function scanMarkdown(text: string): ScanResult {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let paragraph: string[] = [];
  let paragraphRaw: string[] = [];
  let paragraphStart = 0;

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      blocks.push({
        kind: 'para',
        line: paragraphStart,
        text: paragraph.join(' ').trim(),
        links: extractLinks(paragraphRaw.join(' ')),
      });
      paragraph = [];
      paragraphRaw = [];
    }
  };

  for (let i = 0; i < lines.length; i += 1) {
    const raw = lines[i] ?? '';
    const lineNo = i + 1;
    const trimmed = raw.trim();

    if (trimmed === '') {
      flushParagraph();
      continue;
    }

    // Fenced block: consume verbatim until the closing fence.
    const fence = FENCE.exec(raw);
    if (fence) {
      flushParagraph();
      const marker = fence[1]!;
      const lang = (fence[2] ?? '').toLowerCase();
      const body: string[] = [];
      let j = i + 1;
      for (; j < lines.length; j += 1) {
        const candidate = (lines[j] ?? '').trim();
        if (candidate.startsWith(marker[0]!.repeat(3)) && candidate.replace(/[`~]/g, '') === '') break;
        body.push(lines[j] ?? '');
      }
      blocks.push({ kind: 'code', line: lineNo, lang, body: body.join('\n') });
      i = j;
      continue;
    }

    const heading = HEADING.exec(raw);
    if (heading) {
      flushParagraph();
      blocks.push({
        kind: 'heading',
        line: lineNo,
        level: heading[1]!.length,
        text: cleanInline(heading[2]!),
      });
      continue;
    }

    // Table: a pipe row immediately followed by a separator row.
    if (trimmed.includes('|') && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1] ?? '') && (lines[i + 1] ?? '').includes('-')) {
      flushParagraph();
      const header = splitTableRow(raw);
      const rows: TableRow[] = [];
      let j = i + 2;
      for (; j < lines.length; j += 1) {
        const rowLine = lines[j] ?? '';
        if (rowLine.trim() === '' || !rowLine.includes('|')) break;
        if (HEADING.test(rowLine) || BULLET.test(rowLine)) break;
        const parsed = splitTableRowWithLinks(rowLine);
        if (parsed.cells.length === 1 && parsed.cells[0] === '') continue;
        rows.push({ cells: parsed.cells, links: parsed.links, line: j + 1 });
      }
      blocks.push({ kind: 'table', line: lineNo, header, rows });
      i = j - 1;
      continue;
    }

    const bullet = BULLET.exec(raw);
    if (bullet) {
      flushParagraph();
      blocks.push({
        kind: 'bullet',
        line: lineNo,
        indent: (bullet[1] ?? '').replace(/\t/g, '  ').length,
        text: cleanInline(bullet[2]!),
        links: extractLinks(bullet[2]!),
      });
      continue;
    }

    if (paragraph.length === 0) paragraphStart = lineNo;
    paragraph.push(cleanInline(trimmed));
    paragraphRaw.push(trimmed);
  }

  flushParagraph();
  return { blocks, lines };
}

const MARKDOWN_LINK = /\[([^\]]*)\]\(\s*<?([^)\s>]+)>?\s*\)/g;

/**
 * `cleanInline` turns `[Hub](http://…)` into `Hub`, which is right for names and
 * wrong for URLs. Every block therefore carries the targets separately, so the
 * map's Markdown links survive without polluting the text.
 */
export function extractLinks(raw: string): string[] {
  const urls: string[] = [];
  MARKDOWN_LINK.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = MARKDOWN_LINK.exec(raw)) !== null) {
    const target = (match[2] ?? '').trim();
    if (/^(?:https?:\/\/|mailto:)/i.test(target)) urls.push(target);
  }
  return urls;
}
