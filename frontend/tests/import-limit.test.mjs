import assert from 'node:assert/strict';
import test from 'node:test';
import { appRunner, get, load, text } from './state-harness.mjs';

function openImportForm(runner) {
  const button = get(runner.render(), (node) => node.type === 'Button' && text(node) === 'Новая встреча');
  button.props.onClick();
  return runner.render();
}

test('new meeting import shows server byte cap beside the enforced duration cap', () => {
  const runner = appRunner();
  runner.model.config = { ...runner.model.config, max_upload_bytes: 2 * 1024**3 };
  const tree = openImportForm(runner);
  const hint = get(tree, (node) => node.type === 'p' && text(node).includes('Аудио или видео'));
  assert.equal(text(hint), 'Аудио или видео · до 4 часов · максимум 2 ГиБ');
});

test('new meeting import does not invent a byte cap when server config is unavailable', () => {
  const runner = appRunner();
  runner.model.config = null;
  const tree = openImportForm(runner);
  const hint = get(tree, (node) => node.type === 'p' && text(node).includes('Аудио или видео'));
  assert.equal(text(hint), 'Аудио или видео · до 4 часов · лимит размера не получен');
});

test('binary byte formatting uses precise display units', () => {
  const { formatBinaryBytes } = load('utils/format.ts', {});
  assert.equal(formatBinaryBytes(2 * 1024**3), '2 ГиБ');
  assert.equal(formatBinaryBytes(1536), '1,5 КиБ');
  assert.equal(formatBinaryBytes(Number.MAX_SAFE_INTEGER + 1), 'Неизвестно');
});
