import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import test from 'node:test';
import vm from 'node:vm';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';
import * as model from '../src/team/model.ts';

const require = createRequire(import.meta.url);
const MARKER = 'secretary-origin:00000000-0000-4000-8000-000000000003';
const actor = { id: 'owner', display_name: 'Владелец', role: 'owner', project_ids: ['7'], revision: 1 };
const sourceUrl = new URL('../src/team/TeamApp.tsx', import.meta.url);
const projectionUrl = new URL('../src/team/taskDescription.ts', import.meta.url);
const projection = fs.existsSync(projectionUrl) ? await import(projectionUrl.href) : null;
const source = fs.readFileSync(sourceUrl, 'utf8');
const compiled = ts.transpileModule(source, {
  fileName: 'TeamApp.tsx',
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;

// Exercise the complete production component and real React text escaping.
// Only its external hooks/children are replaced by inert synthetic fixtures.
function renderDescription(description) {
  const selected = { task_id: '1', project_id: '7', revision: 1, title: 'Проверка описания', description,
    assignee_id: actor.id, bucket: 'accepted', due_at: null, due_confirmed: true,
    important: false, urgent: false, classification_confirmed: true, origin: null };
  const state = { projectId: '7', selectedTask: selected, tasks: [selected], members: [], operations: [],
    history: [], draft: model.emptyDraft(), preview: null, loading: false, detailLoading: false };
  const emptyComponent = () => null;
  const hooks = {
    './useTeamAuth': { useTeamAuth: () => ({ state: { actor, sessionEpoch: 1 }, auth: {} }) },
    './useTeamTasks': { useTeamTasks: () => ({ state, store: {} }) },
  };
  const module = { exports: {} };
  const sandbox = { module, exports: module.exports, URLSearchParams, window: { location: { search: '' } },
    require: (name) => {
      if (name === 'react' || name === 'react/jsx-runtime') return require(name);
      if (name === './model') return model;
      if (name === './api') return { createTeamApi: () => ({}) };
      if (name === './taskDescription') return projection;
      if (hooks[name]) return hooks[name];
      if (name === 'lucide-react') return Object.fromEntries(['AlertCircle', 'CheckCircle2', 'Clock3', 'LayoutGrid', 'LoaderCircle', 'RefreshCw', 'Users', 'X'].map((key) => [key, emptyComponent]));
      if (name.startsWith('./Team')) return { [name.slice(2)]: emptyComponent };
      throw new Error(`Unexpected TeamApp dependency: ${name}`);
    } };
  vm.runInNewContext(compiled, sandbox, { filename: 'TeamApp.test-harness.cjs' });
  const html = renderToStaticMarkup(React.createElement(module.exports.TeamApp));
  const descriptionHtml = html.match(/<p class="team-detail-description">([\s\S]*?)<\/p>/)?.[1] ?? '';
  assert.equal(selected.description, description, 'display must not mutate the canonical source description');
  return descriptionHtml;
}

function textMarkup(value) {
  return value.replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;' })[char]);
}

function expectDescription(input, expected) {
  assert.equal(renderDescription(input), textMarkup(expected));
}

test('the open TeamApp card displays generated description text and hides its service line', () => {
  expectDescription(`<p>Текст &amp; договорённость<br>Вторая строка 🙂</p><p>${MARKER}</p>`, 'Текст & договорённость\nВторая строка 🙂');
});

test('escaped user markup stays literal and is never reparsed after entity decoding', () => {
  expectDescription(`<p>&lt;p&gt;Текст &amp; &lt;script&gt;пример&lt;/script&gt;&lt;/p&gt;<br>&amp;lt;b&amp;gt;</p><p>${MARKER}</p>`, '<p>Текст & <script>пример</script></p>\n&lt;b&gt;');
});

test('generated wrappers preserve user spaces, leading blank lines and trailing blank lines', () => {
  expectDescription(`<p><br>  Первый<br><br>Последний  <br><br></p><p>${MARKER}</p>`, '\n  Первый\n\nПоследний  \n\n');
});

test('only the canonical lowercase origin marker on a whole visible line is hidden', () => {
  const retained = [`prefix ${MARKER}`, `${MARKER} explanation`, MARKER.replace('secretary-origin', 'Secretary-origin'),
    'secretary-origin:ABCDEFAB-0000-4000-8000-000000000003', 'secretary-origin:not-a-uuid',
    `${MARKER}0`, 'secretary-command:00000000-0000-4000-8000-000000000003:0'];
  expectDescription(`<p>${retained.join('<br>')}</p><p>${MARKER}</p>`, retained.join('\n'));
});

test('standalone marker matching ignores surrounding visible whitespace and leaves adjacent text', () => {
  expectDescription(`<p>До</p><div> &#160;${MARKER}\t </div><p>После</p>`, 'До\nПосле');
});

test('paragraph, division, list, preformatted and br boundaries preserve readable lines', () => {
  expectDescription('<p>Один <b>важный</b></p><div>Два<br>Три</div><ul><li>Четыре</li><li>Пять</li></ul><pre>  Шесть\n Семь</pre>', 'Один важный\nДва\nТри\nЧетыре\nПять\n  Шесть\n Семь');
});

test('raw script/style data and resource attributes never become description DOM', () => {
  const input = '<p>До<img src="https://invalid.example/x" onerror="alert(1)"><a href="javascript:alert(1)">ссылка</a><script>alert(2)</script><style>body{display:none}</style><br>&lt;img src=&quot;x&quot; onerror=&quot;alert(3)&quot;&gt;</p>';
  const html = renderDescription(input);
  assert.equal(html, textMarkup('Доссылка\n<img src="x" onerror="alert(3)">'));
  assert.doesNotMatch(html, /<(?:img|script|style|a|iframe)\b/i);
});

test('tag-like or malformed script/style content does not hide the text after its closing tag', () => {
  expectDescription('<p>До<script>"<style>"; "<p title=\'"</script>После<style>"<script>"; "<b title=\'"</style>Конец</p>', 'ДоПослеКонец');
});

test('malformed incomplete tags and raw comparisons remain inert visible text', () => {
  expectDescription('<p>5 < 10 > 3<br>Не закрыто: <b title="x</p', '5 < 10 > 3\nНе закрыто: <b title="x</p');
});

test('quoted greater-than signs in attributes do not consume following user text', () => {
  expectDescription('<p>До<img alt="x>y" src="https://invalid.example/a">После</p>', 'ДоПосле');
});

test('Secretary escape entities and valid Unicode numeric references decode once', () => {
  expectDescription('<p>&quot; &#x27; &#39; &#34; &#38; &#60; &#62; &#x1F642; &amp;copy; &unknown;</p>', '" \' \' " & < > 🙂 &copy; &unknown;');
});

test('invalid numeric Unicode references remain safe replacement characters', () => {
  expectDescription('<p>&#0; &#xD800; &#x110000;</p>', '� � �');
});

test('marker-only and empty descriptions have no user-visible service text', () => {
  expectDescription(`<p></p><p>${MARKER}</p>`, '');
  expectDescription('', '');
});

test('plain description text preserves all literal whitespace and unknown entities', () => {
  expectDescription('  Простой текст\r\n\r\n5 < 10 &unknown;  ', '  Простой текст\n\n5 < 10 &unknown;  ');
});
