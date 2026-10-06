import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { createScratchRun } from './fixtures/scratch-path.mjs';

function filesystem(root, { symbolic = null, redirected = null } = {}) {
  const writes = [];
  return { writes, existsSync: () => true,
    lstatSync: value => ({ isDirectory: () => true, isSymbolicLink: () => value === symbolic }),
    realpathSync: value => value === redirected ? path.join(root, 'foreign') : value,
    mkdirSync: value => { writes.push(['mkdir', value]); },
    mkdtempSync: value => { writes.push(['mkdtemp', value]); return value + 'unique'; } };
}

test('a scratch ancestor junction is refused before any directory creation', () => {
  const root = path.resolve('synthetic-project');
  const fs = filesystem(root, { symbolic: path.join(root, '.runtime') });
  assert.throws(() => createScratchRun(root, fs), /scratch_path_redirect/);
  assert.deepEqual(fs.writes, []);
});

test('a physical ancestor outside its lexical location is refused before any write', () => {
  const root = path.resolve('synthetic-project');
  const fs = filesystem(root, { redirected: path.join(root, '.runtime', 'team-rollout') });
  assert.throws(() => createScratchRun(root, fs), /scratch_path_redirect/);
  assert.deepEqual(fs.writes, []);
});

test('an ordinary local scratch run stays inside the allowed parent', () => {
  const root = path.resolve('synthetic-project');
  const fs = filesystem(root);
  const run = createScratchRun(root, fs);
  assert.equal(path.dirname(run), path.join(root, '.runtime', 'team-rollout'));
  assert.deepEqual(fs.writes.map(row => row[0]), ['mkdir', 'mkdtemp']);
});
