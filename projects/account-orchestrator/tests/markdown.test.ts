import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cleanInline, scanMarkdown, splitTableRow } from '../src/parse/markdown.js';

test('splits a table row and keeps escaped pipes inside a cell', () => {
  assert.deepEqual(splitTableRow('| a | b \\| c | d |'), ['a', 'b | c', 'd']);
});

test('cleanInline strips bold and code but leaves snake_case alone', () => {
  assert.equal(cleanInline('**GitHub** `jason_smith`'), 'GitHub jason_smith');
  assert.equal(cleanInline('main_rig_tool'), 'main_rig_tool');
});

test('recognises a GFM table with a following separator', () => {
  const { blocks } = scanMarkdown(['| Machine | IP |', '| --- | --- |', '| Acer | 100.64.1.10 |'].join('\n'));
  assert.equal(blocks.length, 1);
  const block = blocks[0]!;
  assert.equal(block.kind, 'table');
  if (block.kind !== 'table') return;
  assert.deepEqual(block.header, ['Machine', 'IP']);
  assert.equal(block.rows.length, 1);
  assert.deepEqual(block.rows[0]!.cells, ['Acer', '100.64.1.10']);
  assert.equal(block.rows[0]!.line, 3);
});

test('records headings, bullets and fenced blocks with line numbers', () => {
  const text = ['# Map', '', '## Machines', '- Acer — 100.64.1.10', '', '```yaml', 'a: b', '```'].join('\n');
  const { blocks } = scanMarkdown(text);
  const kinds = blocks.map((block) => block.kind);
  assert.deepEqual(kinds, ['heading', 'heading', 'bullet', 'code']);
  const code = blocks[3]!;
  assert.equal(code.kind, 'code');
  if (code.kind === 'code') {
    assert.equal(code.lang, 'yaml');
    assert.equal(code.body, 'a: b');
  }
});

test('a pipe line without a separator is prose, not a table', () => {
  const { blocks } = scanMarkdown('one | two\nthree');
  assert.equal(blocks[0]!.kind, 'para');
});
