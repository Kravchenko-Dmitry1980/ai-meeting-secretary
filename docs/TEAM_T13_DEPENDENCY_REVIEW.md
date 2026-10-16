# T13: bounded contracts, billing и dependency review

Дата: 2026-10-04. Статус инвентаризации: **completed with explicit gaps**. Это локальный review выбранных границ и разрешённого пакета; не полный security scan, юридическое заключение или разрешение production activation.

## Scope и метод

Основание: canonical plan T13 и spec §§6–7, 10. Прочитаны текущие MAX auth/session/invitation boundary, public Gateway, `MonthlyBudget`, Billing repository, Polza account/paid dispatch, restore/maintenance paid guards, manifests и dependency metadata. Первичная проверка была read-only; подтверждённые находки переданы root с синтетическим RED. После отдельного разрешения root выполнен ограниченный ремонт Voice checkpoint boundary, описанный ниже; исходный Billing repair выполнялся другим агентом. Общий T13 regression принадлежит root.

Из skill [security-diff-scan](C:/Users/user/.codex/plugins/cache/openai-curated-remote/codex-security/0.1.31/skills/security-diff-scan/SKILL.md) использовано руководство по проверке изменений и доказательству дефектов. Его требования «Resolve the exact Git range or local patch and keep it unchanged» и «Finish only after every changed file and candidate is accounted for» здесь полностью не исполнялись: эта карточка имеет согласованный ограниченный scope. Desktop scan start, capability/config preflight, полный changed-file inventory, threat-model и durable scan pipeline не запускались. Это не утверждение о том, что pipeline обязательно передаёт код наружу; full plugin qualification не заявляется. Проверка завершения следует `superpowers:verification-before-completion`: PASS относится только к реально выполненным checks ниже.

Owner `.env`, рабочие DB/audio, чужие процессы и произвольные `.runtime` каталоги не читались. Установка, обновление зависимостей, Git, provider calls и host changes не выполнялись. Единственное внешнее чтение — официальный LICENSE точной версии SentencePiece, указанный ниже. Секреты не выводились и не включались в evidence.

## Выполненное evidence

Каталог: `.runtime/team-rollout/review-t13-contracts-20261004/`.

| Проверка | Результат | Evidence |
| --- | --- | --- |
| Fresh read-only dependency, license, pinned artifact и bounded secret-pattern inventory | exit 0 | `inventory.py`, `inventory.json`, `inventory-output.txt` |
| 6 inventory assertions + 6 auth + 6 billing/paid authority regressions | **18 PASS, 1,81 с** | `bounded-review-green.txt`, `test_inventory_review.py` |
| Финальная budget authority после reservation/checkpoint | **4 RED**, actual MockTransport POST произошёл во всех случаях | `test_billing_dispatch_review.py`, `billing-dispatch-red-v3.txt` |
| Независимый rerun тех же 4 regressions после frozen repair | **4 PASS, 0,72 с**, все 4 изменения proof отклонены до POST | `billing-dispatch-independent-green.txt` |
| Actual VoiceService после нового final budget отказа | **4 RED, 1,74 с**: POST 0, Billing released, но Voice uncertain | `test_voice_final_dispatch_review.py`, `voice-final-dispatch-red.txt` |
| Авторизованный Voice repair + affected tests + исходные 4 RED cases | **278 PASS, 21,31 с** | `voice-fix-affected.txt`; source hashes `voice-source-freeze.json` |

`inventory.json` содержит SHA-256 всех прочитанных lock/manifests и разрешённых package/log файлов. Inventory assertions повторно сравнивают эти hashes с текущими байтами. Это защищает конкретное evidence от незаметной смены входов, но не подтверждает происхождение всех установленных бинарных байтов.

## Подтверждённая находка

**P2 — budget proof проверялся до checkpoint, но не при окончательном dispatch.** Между `ensure_account/reserve` и `MonthlyBudget.mark_submitted` могли произойти: истечение 60 секунд, переход billing period в 01:00 МСК, refresh с cap 4 000 ₽ либо исчерпание текущего остатка. `mark_submitted` проверял только статус reservation и активный key. Четыре независимые synthetic ветки воспроизвели отправку POST при уже недопустимом proof.

**Закрыто после независимой проверки.** Repair в `application/monthly_budget.py` атомарно перепроверяет текущий period/account freshness/cap и affordability перед `reserved → submitted`. Для расчёта доступности только собственный ещё неотправленный reserve исключается до clamp; другие reservations, uncertain/observed costs и уже подтверждённые receipts сохраняются. Scope affordability также перепроверяется в integer micro-RUB. При отказе существующий Polza boundary освобождает только неотправленный reserve. Независимые 4 regression cases прошли без изменения expectations, HTTP POST не вызван. Reviewed source SHA-256: `2d5dff143e3919a20d176c7a947b7f301a111efc19dde15c091fb80367b445cb`. Исправляющий агент отдельно сообщил 145 PASS affected tests; это его evidence, а не повторный полный прогон данной карточки. Новых подтверждённых незакрытых findings в её scope не осталось.

В ограниченном auth source review новых подтверждённых дефектов не найдено. Проверены canonical MAX HMAC/replay, стабильность replay при rotation session secret, attempts/TTL, revision-pinned session, invitation creator/scope, Host/Origin/cookie/CSRF, bounded body/rate и отсутствие публичных admin routes. Из них выбранные 6 durable regressions действительно выполнены; весь auth suite в этой карточке не повторялся.

Billing review также проверил reserve/receipt/key identity, unknown-cost carry, overlap между aggregate и individual receipts, account freshness, restore blocking и maintenance/cancellation lifetime. Пройденные tests не превращают каталог цен в гарантированный верхний счёт и не доказывают реальные настройки provider-side cap.

### Последующий Voice checkpoint regression

Новая проверка actual VoiceService воспроизвела отдельный P2: `begin_request` уже сохранял request, затем final budget отказ освобождал Billing reserve без POST. Voice считал наличие request без response неопределённой оплаченной отправкой: все 4 случая давали `uncertain`, misleading reply и такое же состояние после restart, хотя POST count = 0, Billing `released`, reserve = 0 и provider IDs отсутствовали.

Root отдельно разрешил узкий ремонт. В Polza добавлен optional `not_submitted_checkpoint`, вызываемый только в ветке отказа `mark_submitted`, когда `release_unsubmitted` успешно завершился и `_raw_request` ещё не запускался. Передаются точные operation/category/command/stage/attempt/request_hash + фиксированный error code. Сам статус Billing `released` не трактуется как доказательство отсутствия POST: реальный HTTP 429 тоже может завершить reserve этим статусом.

Team schema **9** добавляет immutable `voice_request_aborts`, FK к исходному request и SQL guards UPDATE/DELETE/REPLACE и response/abort mutual exclusion. Voice проверяет scope, поля/типы, hash, fencing и идемпотентность. Ни request, ни response не удаляются/переписываются; фиктивный HTTP status/response не создаётся. `_pending` исключает только проверенное abort evidence. Известный отказ становится `paused_budget` или `paused_config`; абортированная попытка не возобновляется автоматически и не может считаться complete.

Истёкшая lease и scoped dead-gateway recovery используют одно правило: persisted abort → соответствующий paused status; request без response/abort → `uncertain`. При crash между Billing release и abort checkpoint, ошибке/cancellation abort callback либо реально неизвестном provider outcome сохраняется консервативный `uncertain`. Это не авторизация retry. Raw preflight не создаёт/мигрирует source stores; schemas 7/8 без новой таблицы остаются поддержаны. Новая таблица включена в recovery watermark; отсутствие её в schema9 считается повреждением. Active rows без maintenance ticket по-прежнему отклоняются до writes.

Новые `tests/test_voice_final_dispatch.py`: **39 cases** — обе actual STT/intent ветки для exhausted/config/stale/rollover, restart, timeout/accepted response, HTTP rejection, сохранение первого оплаченного response при отказе repair, failed release, abort write/cancel/crash gaps, ID/scope/hash/fence/idempotency, immutable guards, schema8→9 preservation/rollback, raw schemas7/8 preflight, no-ticket refusal и gateway recovery watermark. Только временные БД, synthetic WAV и HTTPX MockTransport; owner audio/providers не использовались. Вместе с затронутыми Polza/Voice suites и исходными 4 scratch regressions получено **278 PASS**.

Независимый reviewer затем повторил исходные 4 неизменённых scratch regressions и 39 permanent cases: **43 PASS, 13,44 с**, все 6 frozen source hashes совпали. В read-only проверке proof bindings, immutable/exclusive abort ledger, raw schema7/8 fallback, schema9 integrity и unknown no-replay новых подтверждённых findings не найдено. Его evidence: `.runtime/team-rollout/t13-voice-independent-review-f3fca29431234516b473ce592c56cbc4/evidence.json`. Отдельный compatibility fixture agent сообщил **295 PASS** на совпадающих frozen `TeamDatabase`/`VoiceRepository`/`TeamRuntimeRecovery` hashes. Это результаты других агентов, не повторный полный прогон данной карточки; полный backend run фиксирует root.

## Dependency consistency

| Контур | Проверенный результат |
| --- | --- |
| Main Python | `requires-python >=3.12,<3.13`, lock `==3.12.*`; **32 lock entries / 32 installed distributions**, совпадение имён/версий без extras; все 11 direct runtime/dev declarations удовлетворены; все registry artifacts имеют SHA-256; license inventory versions совпадают |
| Frontend | package-lock v3; **274 entries**, все с SHA-512 integrity; 21 direct declarations совпадают с root lock и resolved versions; **243 installed** совпадают по версиям; 31 отсутствующая запись — optional platform/wasm dependency |
| Optional CPU voice | отдельный requirements lock, **35 pins / 35 installed distributions**, версии и license inventory совпадают; каждый requirement block имеет hash; в main lock не добавлялся |

Main direct resolved versions: FastAPI 0.142.2; uvicorn 0.54.0; Pydantic 2.13.5; pydantic-settings 2.15.0; HTTPX 0.28.1; python-multipart 0.0.32; python-docx 1.2.0; psutil 7.2.2; PyAudioWPatch 0.2.12.8; pytest 8.4.2; pytest-asyncio 0.26.0.

Это проверка деклараций/lock/installed **metadata**. Download/install/rebuild и сравнение всего installed code с исходными wheels/tarballs не выполнялись. У `build-system.requires = ["hatchling"]` нет точного pin в main lock; изолированная wheel build environment этим inventory не воспроизведена. CVE/advisory feeds, transitive native code provenance и reproducible build не проверялись. Optional voice model manifest закрепляет revision и hashes, но model bytes в данной карточке не открывались.

## License/notice inventory

Существующие `docs/licenses/inventory.json` и `config/voice-runtime/license-inventory.json` сверены с локальными metadata. Все уже перечисленные retained notice paths существуют. Отсутствие notice в inventory не объявляется нарушением лицензии.

- Main Python: 31 сторонняя distribution имеет сохранённый notice; собственная `secretary 0.1.0` не имеет выбранной license metadata/notice. Лицензию приложения определяет правообладатель; private pilot не квалифицируется этим как licensed public distribution.
- npm: у **233 из 274** lock entries есть retained notices. **41 без notice**: 31 optional dependency отсутствует в installed runtime; ещё 10 установлены как dev/build dependencies, перечислены ниже. Все 5 direct UI runtime dependencies имеют сохранённые notices.
- CPU voice: **33 из 35** distributions имеют retained notices. `sentencepiece 0.2.2` имеет metadata `Apache-2.0`, но в installed wheel нет отдельного license file. [Официальный LICENSE v0.2.2](https://raw.githubusercontent.com/google/sentencepiece/v0.2.2/LICENSE) прочитан и содержит Apache License 2.0; отдельная upstream notice/source поставка не архивировалась. `tqdm 4.70.1` metadata: `MPL-2.0 AND MIT`; его локальный `LICENCE` существует, но inventory collector пропустил British spelling. Дополнительный notice сохранён в приложении к этому отчёту без изменения прежнего inventory.

Дополнительный source record для 10 installed dev/build dependencies без retained notice:

| Exact package | Declared license в installed package.json | Source repository из metadata |
| --- | --- | --- |
| @humanfs/types 0.15.0 | Apache-2.0 | github.com/humanwhocodes/humanfs |
| @redocly/openapi-core 1.34.20 | MIT | github.com/Redocly/redocly-cli |
| @rolldown/binding-win32-x64-msvc 1.0.0-rc.17 | MIT | github.com/rolldown/rolldown |
| change-case 5.4.4 | MIT | github.com/blakeembrey/change-case |
| dlv 1.1.3 | MIT | developit/dlv |
| esrecurse 4.3.0 | BSD-2-Clause | github.com/estools/esrecurse |
| imurmurhash 0.1.4 | MIT | github.com/jensyt/imurmurhash-js |
| keyv 4.5.4 | MIT | github.com/jaredwray/keyv |
| natural-compare 1.4.0 | MIT | github.com/litejs/natural-compare-lite |
| uri-js-replace 1.0.1 | MIT | github.com/andreinwald/uri-js-replace |

Эти repository identifiers не являются доказательством проверки точного source tag. Новые upstream code/licenses для этих 10 build dependencies не загружались. Они не объявляются составом публичного browser bundle только из-за наличия в node_modules. Для отдельной распространяемой build toolchain нужен её полный notice package; этот review его не подменяет.

## Pinned artifacts

Прочитаны только конкретные ранее подготовленные local fixtures; бинарники не исполнялись.

| Artifact | Actual SHA-256, совпадает с manifest |
| --- | --- |
| Vikunja 2.7.0 Windows amd64 executable, `.runtime/team/t1-wrapper-verified/vikunja-v2.7.0-windows-4.0-amd64.exe` | `e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6` |
| Caddy 2.11.7 Windows amd64, `.runtime/team-rollout/t11-caddy-2.11.7/binary/caddy.exe` | `5f93a9bd555f3aac9d33af2756ba14b3519d8cca4ffd55256bdaeab444c9db2a` |
| `docs/contracts/vikunja-v2.openapi.json` | `3636e5083f4df9a2110a9c9165cfe0cccbdc77743e61bbf1c95650537920fc75` |

Vikunja local LICENSE — GNU AGPL v3 text, SHA-256 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`; integration manifest declares AGPL-3.0-or-later. Caddy local LICENSE — Apache 2.0, SHA-256 `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`. ZIP hashes, original GPG signature and publisher identity verification are retained T1/T11 evidence; они не повторялись в T13. Manifest hash совпадение не заменяет проверку подписи/происхождения или compliance review выбранной формы распространения.

## Explicit bundle/log/env hygiene scope

Package: `.runtime/team-rollout/t12-ui-final-a161ae2441ef43ccafb6b69d1d209680/public` — **4 public files, 319 046 bytes**; `deploy-manifest.json` и `asset-routes.caddy` — ещё 2 приватных служебных файла. SHA-256/size каждого public file и routes файла совпадают с deploy manifest. Все 6 файлов были включены в bounded text scan; наличие двух служебных файлов рядом не означает разрешение выдавать их HTTP.

Точные 7 logs: `t12-accepted-final-v3-backend.txt`, `t12-runtime-review/final-evidence-9.txt`, `t12-backup-containment-final-38.txt`; в указанном UI parent — `tests.txt`, `lint.txt`, `build.txt`, `pack-tests.txt`.

**0 hits** по strong patterns: private-key headers, длинные sk-/tk_ tokens, JWT, credential-bearing HTTP URL, непустые длинные значения именованных MAX/Polza/Team secrets. Значения matches не сохранялись. Это эвристическая проверка точного набора файлов: она не доказывает отсутствие произвольно закодированного секрета, не сканирует owner configuration и не обещает чистоту будущих logs.

`config/team/.env.team.example`: все **13 values пусты**. Legacy `.env.example`: secret fields пусты, 7 nonsecret defaults оставлены (URL/model/chunk/timeout/legacy meeting budget/flags); поэтому весь legacy example не называется полностью пустым. Фактические `.env` не читались. Legacy `MEETING_BUDGET_RUB` не является новым месячным budget policy; новый путь использует общий Billing guard.

External MAX bridge имеет `integrity: null`, `pinning: official_unversioned_cdn` и exact URL `https://st.max.ru/js/max-web-app.js`. Это уже документированная граница доверия в `TEAM_PUBLIC_ASSETS.md`, а не hash-pinned code. JS с CDN в этом review не загружался. Package hashes покрывают только локальные файлы; полный исполняемый browser supply chain не объявляется закрытым.

## Remaining gates

Не выполнены owner authenticated MAX/Polza, native voice/phone/WebView/cookies, external HTTPS443/DNS/ingress, реальный provider cap и account reconciliation, hardware, 24h pilot, полная license/source compliance перед распространением, vulnerability feed scan или полный formal security pipeline. Итог T13 и разрешение следующей стадии принадлежат `TEAM_VALIDATION.md`/root acceptance, а не этому ограниченному review.

## Дополнение: установленный tqdm 4.70.1 LICENCE

Source: `.venv-voice/Lib/site-packages/tqdm-4.70.1.dist-info/licenses/LICENCE`, 1 985 bytes, SHA-256 `fcff87c3a47ce8028a8512aa182d4fcf0ad1c90544ee75cf9b343684cac194de`. Ниже копия текста notice; она не изменяет license metadata приложения.

<pre>`tqdm` is a product of collaborative work.
Unless otherwise stated, all authors (see commit logs) retain copyright
for their respective work, and release the work under the MIT licence
(text below).

Exceptions or notable authors are listed below
in reverse chronological order:

* files: *
  MPL-2.0 2015-2026 (c) Casper da Costa-Luis
  [casperdcl](https://github.com/casperdcl).
* files: tqdm/_tqdm.py
  MIT 2016 (c) [PR #96] on behalf of Google Inc.
* files: tqdm/_tqdm.py README.rst .gitignore
  MIT 2013 (c) Noam Yorav-Raphael, original author.

[PR #96]: https://github.com/tqdm/tqdm/pull/96


Mozilla Public Licence (MPL) v. 2.0 - Exhibit A
-----------------------------------------------

This Source Code Form is subject to the terms of the
Mozilla Public License, v. 2.0.
If a copy of the MPL was not distributed with this project,
You can obtain one at https://mozilla.org/MPL/2.0/.


MIT License (MIT)
-----------------

Copyright (c) 2013 noamraph

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the &quot;Software&quot;), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED &quot;AS IS&quot;, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
</pre>
