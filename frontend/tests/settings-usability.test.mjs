import assert from 'node:assert/strict';
import test from 'node:test';
import { config, flush, get, hookRuntime, load, text, walk } from './state-harness.mjs';

const cloudControl = (tree) => get(tree, (node) => node.type === 'label' && text(node) === 'Обрабатывать записи через Polza').props.children[0];
const submit = (tree) => get(tree, (node) => node.type === 'form').props.onSubmit({ preventDefault() {} });

function panel(overrides = {}, onSave = async () => true) {
  const runtime = hookRuntime();
  const configModule = load('utils/config.ts', {});
  const { SettingsPanel } = load('components/SettingsPanel.tsx', {
    react: runtime.react,
    '../utils/config': configModule,
    './CloudBudgetPanel': { CloudBudgetPanel: 'CloudBudgetPanel' },
  });
  const props = { config: { ...config, ...overrides }, catalog: null, busy: false, onClose() {}, onSave };
  return { render: () => runtime.render(() => SettingsPanel(props)) };
}

function visibleNodes(node) {
  if (Array.isArray(node)) return node.flatMap(visibleNodes);
  if (!node || typeof node !== 'object') return [];
  const children = node.type === 'details' && !node.props.open
    ? [].concat(node.props.children).filter((child) => child?.type === 'summary')
    : node.props?.children;
  return [node, ...visibleNodes(children)];
}

test('configured key needs no reentry and the default settings view has one processing control', () => {
  const ui = panel({ runtime_outbound_enabled: true });
  const tree = ui.render();
  assert.match(text(tree), /Polza подключена/);
  assert.match(text(tree), /Повторно вводить его не нужно/);
  assert.doesNotMatch(text(tree), /POLZA_API_KEY|Secretary\\.env|Открыть ключи Polza/);
  const visible = visibleNodes(tree);
  assert.equal(visible.filter((node) => node.type === 'input').length, 1);
  assert.equal(visible.some((node) => node.type === 'select'), false);
  assert.equal(get(tree, (node) => node.type === 'details').props.open, undefined);
  assert.equal(get(tree, (node) => node.type === 'summary').props.children, 'Дополнительные настройки');
  assert.equal(walk(tree).some((node) => node.type === 'CloudBudgetPanel'), false);
  assert.equal(cloudControl(tree).props.disabled, false);
  assert.equal(cloudControl(tree).props.checked, true);
});

test('enabling processing saves only the changed setting, without readonly runtime or key fields', async () => {
  let submitted;
  const ui = panel({ cloud_enabled: false, runtime_outbound_enabled: true }, async (changes) => { submitted = changes; return true; });
  let tree = ui.render();
  cloudControl(tree).props.onChange({ target: { checked: true } });
  tree = ui.render(); submit(tree); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(submitted)), { cloud_enabled: true });
  assert.match(text(ui.render()), /Настройки сохранены/);
});

test('an Offline launch explains the required restart and cannot pretend to enable outbound calls', () => {
  const ui = panel({ cloud_enabled: true, runtime_outbound_enabled: false });
  const tree = ui.render();
  assert.equal(cloudControl(tree).props.disabled, true);
  assert.equal(cloudControl(tree).props.checked, false);
  assert.match(text(tree), /Перезапустите приложение в обычном режиме/);
  assert.match(text(tree), /Сохранённый ключ уже используется/);
  assert.doesNotMatch(text(tree), /POLZA_API_KEY/);
});

test('a genuinely missing key shows one-time connection instructions and keeps processing unavailable', () => {
  const ui = panel({ key_configured: false, runtime_outbound_enabled: true });
  const tree = ui.render();
  assert.match(text(tree), /Polza ещё не подключена/);
  assert.match(text(tree), /Подключите ключ Polza один раз/);
  assert.equal(cloudControl(tree).props.disabled, true);
  assert.equal(get(tree, (node) => node.type === 'a').props.href, 'https://polza.ai/dashboard/api-keys');
});

test('advanced controls remain usable and expense reconciliation mounts only after opening them', async () => {
  let submitted;
  const ui = panel({ local_cost_limits_enabled: true, runtime_outbound_enabled: true }, async (changes) => { submitted = changes; return true; });
  let tree = ui.render();
  const details = get(tree, (node) => node.type === 'details');
  assert.equal(walk(details).filter((node) => node.type === 'select').length, 2);
  assert.ok(walk(details).some((node) => node.type === 'input' && node.props.value === 120));
  assert.match(text(details), /Максимальный тариф STT/);
  assert.match(text(details), /Резерв неизвестной цены/);
  details.props.onToggle({ currentTarget: { open: true } });
  tree = ui.render();
  assert.equal(get(tree, (node) => node.type === 'CloudBudgetPanel').props.enabled, true);
  get(tree, (node) => node.type === 'select' && node.props.value === 'stt').props.onChange({ target: { value: 'new-stt' } });
  tree = ui.render(); submit(tree); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(submitted)), { stt_model: 'new-stt', stt_price_rub_per_minute: null });
});

test('older config responses without runtime status retain normal cloud control compatibility', () => {
  const tree = panel().render();
  assert.equal(cloudControl(tree).props.disabled, false);
  assert.equal(cloudControl(tree).props.checked, true);
});

test('native validation reveals an invalid advanced field instead of leaving it hidden', () => {
  const ui = panel(); const tree = ui.render();
  const details = get(tree, (node) => node.type === 'details');
  const invalidField = get(details, (node) => node.type === 'select');
  const element = { open: false, contains: (target) => target === invalidField };
  details.props.ref.current = element;
  get(tree, (node) => node.type === 'form').props.onInvalidCapture({ target: invalidField });
  assert.equal(element.open, true);
  assert.equal(walk(ui.render()).some((node) => node.type === 'CloudBudgetPanel'), true);
});
