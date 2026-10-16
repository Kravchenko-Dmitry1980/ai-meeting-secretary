import assert from 'node:assert/strict';
import test from 'node:test';
import { flush, get, hookRuntime, load, text } from './state-harness.mjs';

const operation = {
  operation_id: 'synthetic-operation-1', category: 'meeting_stt', period: '2026-10', status: 'uncertain',
  estimated_rub: 2, reserved_rub: 2, observed_rub: null, confirmed_rub: null,
  provider_request_id: 'gen_synthetic', provider_job_id: null, confirmed_period: null,
  created_ms: Date.UTC(2026, 9, 9), updated_ms: Date.UTC(2026, 9, 9),
};
const button = (tree, label) => get(tree, (node) => node.type === 'Button' && text(node) === label);

function panel({ enabled = true, reconcile, category = 'meeting_stt' } = {}) {
  const runtime = hookRuntime(); const calls = [];
  const currentOperation = { ...operation, category };
  const api = {
    cloudBudgetOperations: async (status, limit, offset) => {
      calls.push({ kind: 'list', status, limit, offset });
      return { items: [currentOperation], limit, offset, next_offset: null };
    },
    cloudBudgetOperation: async (id) => {
      calls.push({ kind: 'detail', id }); return { operation: currentOperation, events: [] };
    },
    reconcileCloudBudgetOperation: async (id) => {
      calls.push({ kind: 'reconcile', id }); return reconcile ? reconcile(id) : { operation_id: id, status: 'uncertain', budget: {} };
    },
  };
  const { CloudBudgetPanel } = load('components/CloudBudgetPanel.tsx', {
    react: runtime.react, '../services/api': { api, errorMessage: (error) => error.message },
  });
  return { calls, render: (commitEffects = false) => runtime.render(() => CloudBudgetPanel({ enabled }), commitEffects) };
}

test('cloud spend panel lists only the selected status and keeps exact provider IDs visible', async () => {
  const ui = panel(); ui.render(true); await flush(); const tree = ui.render();
  assert.match(text(tree), /Сверка расходов Polza/);
  assert.match(text(tree), /gen_synthetic/);
  assert.match(text(tree), /Исход не подтверждён/);
  assert.match(text(tree), /Сверка запрашивает только сохранённый ID/);
  assert.deepEqual(ui.calls[0], { kind: 'list', status: 'uncertain', limit: 50, offset: 0 });
  assert.equal(text(button(tree, 'Сверить точный ID')), 'Сверить точный ID');
});

test('cloud-off panel disables reconciliation and does not send a provider request', async () => {
  const ui = panel({ enabled: false }); ui.render(true); await flush(); const tree = ui.render();
  assert.match(text(tree), /Сверка отключена/);
  assert.equal(ui.calls.some((item) => item.kind === 'reconcile'), false);
  assert.equal(get(tree, (node) => node.type === 'Button' && text(node) === 'Обновить').props.disabled, false);
});

test('operator panel keeps historical operation categories visible', async () => {
  const ui = panel({ category: 'short_voice' }); ui.render(true); await flush(); const tree = ui.render();
  assert.match(text(tree), /short_voice/);
});

test('operator reconcile uses the saved operation ID and preserves an uncertain reserve on provider pending', async () => {
  const ui = panel({ reconcile: async (id) => ({ operation_id: id, status: 'uncertain', budget: {} }) });
  ui.render(true); await flush(); let tree = ui.render();
  button(tree, 'Сверить точный ID').props.onClick(); await flush(); await flush(); tree = ui.render();
  assert.deepEqual(ui.calls.filter((item) => item.kind === 'reconcile'), [{ kind: 'reconcile', id: operation.operation_id }]);
  assert.match(text(tree), /Операция остаётся неопределённой/);
  assert.match(text(tree), /повторная оплачиваемая отправка не выполнялась/);
});
