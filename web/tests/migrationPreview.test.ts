import assert from 'node:assert/strict';
import { test } from 'node:test';
import { attachmentFilename } from '../src/utils/migrationPreview';

test('accepts only a safe PHP attachment filename', () => {
  assert.equal(attachmentFilename('attachment; filename="2026_11_02_schema.php"'), '2026_11_02_schema.php');
  assert.equal(attachmentFilename('attachment; filename="../secret.php"'), null);
  assert.equal(attachmentFilename('inline; filename="x.php"'), null);
  assert.equal(attachmentFilename('attachment; filename="x.php\r\nX-Bad: yes"'), null);
});
