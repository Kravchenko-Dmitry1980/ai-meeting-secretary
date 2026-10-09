import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import path from 'node:path';

export type SecretaryReleaseManifest = {
  schema_version: 2;
  backend_version: string;
  frontend_version: string;
  build_check: string;
  lockfiles: {
    'uv.lock': string;
    'frontend/package-lock.json': string;
  };
};

function readTomlVersion(source: string, tableName: string): string {
  let inTable = false;
  for (const line of source.split(/\r?\n/)) {
    const table = line.match(/^\s*\[([^\]]+)\]\s*$/);
    if (table) {
      if (inTable) break;
      inTable = table[1] === tableName;
      continue;
    }
    if (!inTable) continue;
    const version = line.match(/^\s*version\s*=\s*["']([^"']+)["']\s*(?:#.*)?$/);
    if (version) return version[1];
  }
  throw new Error(`Missing version in [${tableName}]`);
}

function sha256(filePath: string): string {
  return createHash('sha256').update(readFileSync(filePath)).digest('hex');
}

export function createSecretaryReleaseManifest(projectRoot: string, mode: string): SecretaryReleaseManifest {
  const backendToml = readFileSync(path.join(projectRoot, 'pyproject.toml'), 'utf8');
  const frontendPackage = JSON.parse(readFileSync(path.join(projectRoot, 'frontend', 'package.json'), 'utf8')) as { version?: unknown };
  const backendVersion = readTomlVersion(backendToml, 'project');
  if (typeof frontendPackage.version !== 'string' || !frontendPackage.version.trim()) {
    throw new Error('Missing frontend package version');
  }
  return {
    schema_version: 2,
    backend_version: backendVersion,
    frontend_version: frontendPackage.version,
    build_check: mode === 'production' ? 'vite-production' : 'vite-nonproduction',
    lockfiles: {
      'uv.lock': sha256(path.join(projectRoot, 'uv.lock')),
      'frontend/package-lock.json': sha256(path.join(projectRoot, 'frontend', 'package-lock.json')),
    },
  };
}
