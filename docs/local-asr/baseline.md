# Model-free baseline: 26 октября 2026

Проверка выполнялась на существующем рабочем дереве с незакоммиченными изменениями. Это baseline выбранных механизмов Secretary, не квалификация нового ASR runtime и не утверждение о полном suite.

## Выполненные проверки

| Набор | Результат | Сохранённое доказательство |
| --- | --- | --- |
| Polza, speaker repository/API, identification, calibration, cancel recovery | **301 passed**, 81.02 s | `.runtime/team-rollout/offline-ad521b3b4cdb42cca0535c5574d9a520/junit.xml` |
| Private voice primitives, STT cap/resume, summary rate retry, budget scopes | **73 passed**, 21.63 s | `.runtime/team-rollout/offline-161db7062060498eafafe716524b8a9d/junit.xml` |

В каждом запуске было одно предупреждение Starlette: текущий TestClient использует устаревающую интеграцию `httpx`. Установка/обновление зависимостей не выполнялись.

Для последующих интеграционных проверок добавлен `scripts/run_local_asr_tests.py`, который вызывает тот же guarded worker отдельным subprocess и не запускает retention pruning. `--help` завершился с кодом 0; smoke `tests/test_voice_calibration.py -q` дал **7 passed**, 0.43 s, evidence `.runtime/team-rollout/offline-3402fa6814d84d58a242808240fd5d91/junit.xml`. Эти 7 уже входят в первый baseline и не увеличивают число уникальных проверок. По умолчанию команда запускает `tests/test_local_asr*.py`; можно передать явный список pytest arguments. Offline guard не является запретом импорта torch: сами выбранные тесты должны оставаться model-free.

Тесты использовали существующий offline guard: сетевые подключения, рабочие `data`, `audio`, `.env`, реальные аудиоустройства и lifecycle scripts запрещены; синтетические WAV, fake embeddings и собственные короткие Python/FFmpeg процессы разрешены. Модели не загружались. Допускаемый `run_owned` проверен на искусственных процессах, это не проверка полноценного GPU runtime.

Запускали внутренний `_worker`, а не `main()` тестового launcher: `main()` дополнительно удаляет старые test payloads по retention policy; для этого read-only baseline такая очистка не требовалась. Каждому запуску `create_run` выделил отдельный каталог и `--basetemp` внутри него. Репозиторные исходники и пользовательские БД аудит не менял.

Точные команды (PowerShell из корня проекта):

```powershell
.\.venv\Scripts\python.exe -B -c "import sys; sys.path.insert(0,'scripts'); from run_offline_tests import _worker, SCRATCH; from offline_test_retention import create_run, initialize_run, finish_run; run=create_run(SCRATCH); initialize_run(run); print('BASELINE_RUN='+str(run),flush=True); result=_worker(run,['tests/test_polza.py','tests/test_speaker_repository.py','tests/test_speaker_api.py','tests/test_voice_identification.py','tests/test_voice_calibration.py','tests/test_cancel_completion_regression.py','tests/test_cancel_review_regression.py','-q']); finish_run(run,result); raise SystemExit(result)"

.\.venv\Scripts\python.exe -B -c "import sys; sys.path.insert(0,'scripts'); from run_offline_tests import _worker, SCRATCH; from offline_test_retention import create_run, initialize_run, finish_run; run=create_run(SCRATCH); initialize_run(run); print('BASELINE_RUN='+str(run),flush=True); result=_worker(run,['tests/test_voice_primitives.py','tests/test_stt_cap_resume.py','tests/test_summary_rate_retry.py','tests/test_cloud_budget_scopes.py','-q']); finish_run(run,result); raise SystemExit(result)"
```

## Проверенные границы существующего кода

- `infrastructure/polza.py::_client`: прямой `httpx.AsyncClient`, `follow_redirects=False`, `trust_env=False`; транспорт по умолчанию установленного HTTPX объявляет `retries=0`. OpenAI SDK в этом пути не используется. `_raw_request_admitted` делает один HTTP запрос; сбои POST становятся `uncertain`, GET может быть повторён с сохранённым ID. Worker дополнительно требует `not uncertain` для автоматического повтора.
- Учёт ответа вызывается до семантической проверки результата; ошибка валидации не освобождает уже подтверждённую оплату. Неизвестная сумма остаётся `None`/`unknown`. Atomic segment save, сохранённые provider ID, checkpoint reuse и cancel/restart без повторного submit покрыты выбранными тестами. Это не обещание восстановления произвольного повреждённого raw HTTP body.
- `api.py::local_security`: все роуты приложения наследуют localhost Host, допустимый Origin и `X-Secretary-Token` для мутаций. Это однопользовательская локальная область, не внешняя авторизация СЕРВЕРА. Будущий `X-NeuroHR-ASR-Service-Token` — отдельный серверный секрет; он не равен браузерному CSRF токену.
- ECAPA manifest закрепляет `speechbrain/spkrec-ecapa-voxceleb`, revision `0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`, embedding weights SHA-256 `0575cb64845e6b9a10db9bcb74d5ac32b326b8dc90352671d345e2ee3d0126a2`, 16 kHz mono и 192 измерения. `tensor_loader.py::verify_pinned` проверяет размеры/hashes перед загрузкой; аудиоподготовка и matching проверены синтетически. Реальные локальные weights в данном baseline не хешировались и не загружались.
- Matching использует `mean-cosine-v1`, отвергает несовместимый model stamp и overlap, не выдаёт `confirmed`; при отсутствии калибровки остаётся `unknown`. Ручной per-segment override и `manual_unset` хранятся отдельно от исходных сегментов и переживают новые автоматические результаты.
- Ограничение существующего `voice_calibration.py`: файл содержит revision/evidence/threshold/margin, а model stamp получает от текущего enrollment runtime. Он не доказывает происхождение калибровки по preprocessing, набору профилей, aggregation и единице решения. Этот baseline не квалифицирует перенос сегментного порога на новый кластер. Автоматические реальные имена нового ASR анализа должны оставаться выключенными без отдельной совместимой калибровки; ручной режим доступен независимо.

## Совместимость donor DTO

Read-only проверен существующий `NeuroHR_Dev/docs/contracts/MEETING_IMPORT_LOCAL_ASR_V2.md` и builder в `scripts/local_asr_bridge.py`. Контракт сохраняет `schema_version=neurohr.local-asr.v2`, `provider`, `language`, `input_sha256`, `transcript_text`, `segments` как число, `segment_items` как массив с `ordinal`, `speaker_label`, `started_ms`, `ended_ms`, `text`, и `warnings`. Неизвестный label — `Спикер неизвестен`; составной пример — `Спикер 1 + Спикер 2`.

Документированный request: `POST /v2/transcriptions`; заголовки `X-NeuroHR-Local-ASR: v2`, `X-NeuroHR-ASR-Service-Token`, filename/language, bounded Content-Length и media Content-Type. Handoff §13.1 добавляет optional `speaker_turns` и `metrics`. Старый donor builder не сохраняет независимые turns. Следовательно, добавление поля в HTTP JSON само по себе не подтверждает новый storage/UI/export round-trip.

Donor checkout прочитан без изменений. Историческая `.agent` запись не использовалась как доказательство действующего desktop deployment или качества моделей.
