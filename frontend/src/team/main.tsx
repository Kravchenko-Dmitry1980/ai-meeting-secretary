import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { TeamApp } from './TeamApp';
import { waitForMaxBridge } from './maxBridge';
import './team.css';

const lifecycle = new AbortController();
const container = document.getElementById('root')!;
let root: ReturnType<typeof createRoot> | null = null;
window.addEventListener('pagehide', () => { lifecycle.abort(); root?.unmount(); }, { once: true });
window.addEventListener('pageshow', (event) => {
  // BFCache restores an aborted lifetime without executing this module again.
  // A fresh document checks the cookie session instead of reusing disposed state.
  if (event.persisted && lifecycle.signal.aborted) window.location.reload();
});
container.textContent = 'Подключаем приложение…';
void waitForMaxBridge({ signal: lifecycle.signal }).then((bridge) => {
  if (lifecycle.signal.aborted || bridge.status === 'cancelled') return;
  root = createRoot(container);
  root.render(<StrictMode><TeamApp maxSource={bridge.max} lifecycleSignal={lifecycle.signal}
    bridgeUnavailable={bridge.status === 'unavailable'} /></StrictMode>);
});
