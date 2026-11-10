import assert from 'node:assert/strict';
import test from 'node:test';
import MarkdownIt from 'markdown-it';
import { describeToolCall, linkCitations, mergeToolCalls } from '../src/utils/agentTools';

const md = new MarkdownIt({ html: false, linkify: true, breaks: true });

test('citation code spans become links with the path and first line', () => {
  const html = linkCitations(md.render('See `routes/auth.php:19` and `app/Models/User.php:6-12`.'));

  assert.match(html, /data-cite-path="routes\/auth\.php" data-cite-line="19"/);
  assert.match(html, /data-cite-path="app\/Models\/User\.php" data-cite-line="6"[^>]*><code>app\/Models\/User\.php:6-12<\/code>/);
});

test('ordinary code spans and urls stay untouched', () => {
  for (const source of ['`npm run dev`', '`localhost:8080`', 'http://example.com:80/x', '`a.b`', '`$x->y:3`']) {
    const html = md.render(source);
    assert.equal(linkCitations(html), html, source);
  }
});

test('markup injected through a code span cannot reach an attribute', () => {
  const html = linkCitations(md.render('`"><img src=x onerror=alert(1)>.php:1`'));

  assert.doesNotMatch(html, /data-cite-path/);
  assert.doesNotMatch(html, /<img/);
});

test('streamed tool calls merge with the final summaries', () => {
  const streamed = [{ id: 'c1', name: 'code_read', arguments: { path: 'a.php' }, status: 'running' as const }];

  const merged = mergeToolCalls(streamed, [
    { id: 'c1', name: 'code_read', status: 'success', duration_ms: 12, truncated: true, error: null },
    { id: 'c2', name: 'code_text_search', status: 'error', error: 'invalid_arguments' },
  ]);

  assert.deepEqual(merged.map((c) => [c.id, c.status, c.truncated]), [['c1', 'success', true], ['c2', 'error', false]]);
  assert.deepEqual(merged[0].arguments, { path: 'a.php' });
  assert.equal(merged[0].durationMs, 12);
  assert.equal(merged[1].error, 'invalid_arguments');
  assert.deepEqual(mergeToolCalls(undefined, 'not a list'), []);
});

test('tool calls read as short sentences', () => {
  assert.equal(describeToolCall({ name: 'code_read', arguments: { path: 'routes/auth.php', start_line: 1, end_line: 100 } }),
    'Read routes/auth.php:1-100');
  assert.equal(describeToolCall({ name: 'code_text_search', arguments: { pattern: 'login', path_glob: 'routes/*' } }),
    'Searched “login” in routes/*');
  assert.equal(describeToolCall({ name: 'code_symbol_lookup', arguments: { symbol: 'User', relation: 'callers' } }),
    'Looked up callers of User');
  assert.equal(describeToolCall({ name: 'project_structure', arguments: { depth: 2 } }), 'Listed structure (depth 2)');
  assert.equal(describeToolCall({ name: 'custom_rest_tool' }), 'custom_rest_tool');
});

test('a URL template yields a starting schema with one required string per placeholder', async () => {
  const { schemaFromUrlTemplate } = await import('../src/utils/agentTools');

  assert.deepEqual(schemaFromUrlTemplate('https://api.example.com/{{ org }}/items/{{id}}?q={{id}}'), {
    type: 'object',
    properties: { org: { type: 'string', description: 'Value for {{org}}.' }, id: { type: 'string', description: 'Value for {{id}}.' } },
    required: ['org', 'id'],
    additionalProperties: false,
  });
  assert.deepEqual(schemaFromUrlTemplate('https://example.com').required, []);
});

test('schema text must be empty or a JSON object schema', async () => {
  const { parseSchemaText } = await import('../src/utils/agentTools');

  assert.deepEqual(parseSchemaText('  '), { schema: null, error: null });
  assert.deepEqual(parseSchemaText('{"type":"object"}'), { schema: { type: 'object' }, error: null });
  assert.match(parseSchemaText('{oops').error!, /not valid JSON/);
  assert.match(parseSchemaText('[]').error!, /JSON object/);
  assert.match(parseSchemaText('{"type":"string"}').error!, /"type": "object"/);
});
