import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'vite';

const frontendRoot = fileURLToPath(new URL('../..', import.meta.url));
const projectRoot = path.resolve(frontendRoot, '..');

function tomlVersion(text, tableName) {
  let inTable = false;
  for (const line of text.split(/\r?\n/)) {
    const table = line.match(/^\s*\[([^\]]+)\]\s*$/);
    if (table) {
      if (inTable) break;
      inTable = table[1] === tableName;
      continue;
    }
    const version = inTable && line.match(/^\s*version\s*=\s*["']([^"']+)["']\s*(?:#.*)?$/);
    if (version) return version[1];
  }
  throw new Error(`missing version in [${tableName}]`);
}

test('production Vite build emits matching Secretary and frontend release versions', async () => {
  const scratch = mkdtempSync(path.join(projectRoot, '.runtime-release-manifest-'));
  const output = path.join(scratch, 'dist');
  try {
    await build({
      configFile: path.join(frontendRoot, 'vite.config.ts'),
      root: frontendRoot,
      logLevel: 'silent',
      build: { outDir: output, emptyOutDir: true },
    });

    const manifestPath = path.join(output, 'secretary-release.json');
    assert.equal(existsSync(manifestPath), true, 'Vite must emit the release manifest');
    const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
    const frontend = JSON.parse(readFileSync(path.join(frontendRoot, 'package.json'), 'utf8'));
    const backend = tomlVersion(readFileSync(path.join(projectRoot, 'pyproject.toml'), 'utf8'), 'project');
    const lockfiles = {
      'uv.lock': createHash('sha256').update(readFileSync(path.join(projectRoot, 'uv.lock'))).digest('hex'),
      'frontend/package-lock.json': createHash('sha256')
        .update(readFileSync(path.join(frontendRoot, 'package-lock.json'))).digest('hex'),
    };
    assert.deepEqual(manifest, {
      schema_version: 2,
      backend_version: backend,
      frontend_version: frontend.version,
      build_check: 'vite-production',
      lockfiles,
    });
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});
