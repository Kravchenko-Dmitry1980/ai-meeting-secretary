/** Official unversioned CDN policy; no SDK hash/version or extra handshake is invented. */
export const MAX_BRIDGE_URL = 'https://st.max.ru/js/max-web-app.js';
export const MAX_BRIDGE_SCRIPT_ID = 'team-max-bridge';
export type MaxBridgeResult = Readonly<{ status: 'available' | 'unavailable' | 'cancelled'; max: unknown | null }>;
export type WaitForMaxBridgeOptions = Readonly<{
  source?: unknown; document?: Document; signal?: AbortSignal; timeoutMs?: number;
}>;

export function waitForMaxBridge(options: WaitForMaxBridgeOptions = {}): Promise<MaxBridgeResult> {
  const signal = options.signal;
  const fallback: MaxBridgeResult = Object.freeze({ status: 'unavailable', max: null });
  const cancelled: MaxBridgeResult = Object.freeze({ status: 'cancelled', max: null });
  if (signal?.aborted) return Promise.resolve(cancelled);
  const timeout = options.timeoutMs ?? 5000;
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 5000) return Promise.resolve(fallback);
  let script: HTMLScriptElement, source: unknown;
  try {
    const page = options.document ?? (typeof document === 'undefined' ? null : document);
    const scripts = page?.querySelectorAll<HTMLScriptElement>(`script[id="${MAX_BRIDGE_SCRIPT_ID}"]`);
    if (!scripts || scripts.length !== 1) return Promise.resolve(fallback);
    script = scripts[0];
    if (script.getAttribute('src') !== MAX_BRIDGE_URL || ![null, '', 'text/javascript'].includes(script.getAttribute('type')))
      return Promise.resolve(fallback);
    source = options.source === undefined ? (typeof window === 'undefined' ? null : window) : options.source;
  } catch { return Promise.resolve(fallback); }
  const capture = (): MaxBridgeResult | null => {
    try {
      if (source === null || typeof source !== 'object' || !('WebApp' in source)) return null;
      const bridge = source.WebApp;
      if (bridge === null || typeof bridge !== 'object' || Array.isArray(bridge)) return fallback;
      // Capture the selected object. A late replacement on window cannot change this source.
      return Object.freeze({ status: 'available', max: Object.freeze({ WebApp: bridge }) });
    } catch { return fallback; }
  };
  const existing = capture();
  if (existing) return Promise.resolve(existing);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: MaxBridgeResult) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      script.removeEventListener('load', loaded);
      script.removeEventListener('error', failed);
      signal?.removeEventListener('abort', aborted);
      resolve(signal?.aborted ? cancelled : result);
    };
    const loaded = () => finish(capture() ?? fallback);
    const failed = () => finish(fallback);
    const aborted = () => finish(cancelled);
    const timer = setTimeout(failed, timeout);
    script.addEventListener('load', loaded);
    script.addEventListener('error', failed);
    signal?.addEventListener('abort', aborted, { once: true });
    if (signal?.aborted) { aborted(); return; }
    const raced = capture();
    if (raced) { finish(raced); return; }
  });
}
