import fs from 'node:fs';
import path from 'node:path';

function canonical(value) {
  const normalized = path.resolve(value.replace(/^\\\\\?\\/, ''));
  return process.platform === 'win32' ? normalized.toLowerCase() : normalized;
}

function noRedirect(value, ports) {
  for (let current = path.resolve(value);;) {
    let info;
    try { info = ports.lstatSync(current); }
    catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (info && (info.isSymbolicLink() || !info.isDirectory()
      || canonical(ports.realpathSync(current)) !== canonical(current))) throw new Error('scratch_path_redirect');
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
}

export function createScratchRun(root, ports = fs) {
  if (!path.isAbsolute(root)) throw new Error('scratch_root_absolute_required');
  const parent = path.join(root, '.runtime', 'team-rollout');
  noRedirect(parent, ports);
  ports.mkdirSync(parent, { recursive: true });
  noRedirect(parent, ports);
  const run = ports.mkdtempSync(path.join(parent, 't13-frontend-canonical-'));
  if (canonical(path.dirname(run)) !== canonical(parent)) throw new Error('scratch_run_scope');
  noRedirect(run, ports);
  return run;
}
