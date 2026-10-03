# Аудит и повторное использование

Проверено 2026-10-01. Secretary был пустым каталогом; в нём создан самостоятельный локальный Git-репозиторий без commit/push. Доноры использовались только для чтения: их окружения, tracked/untracked изменения и процессы сохранены. GitHub-донор изучен в изолированном `.reference/ai-meeting-secretary`; upstream Meetily — через GitHub MCP на закреплённом commit. `.reference` исключена из Git и обычного поиска рабочего кода.

| Источник | Идентичность | Главное заключение | Решение |
|---|---|---|---|
| `meetily` | `restore/P030-migration-snapshot`, `91b0c0985932d0797e249033601afa14f22ee3d3`, только untracked `.agent/` | Active CPAL/WASAPI путь есть; audio_v2 не подключён; unbounded queue, STT gate capture, confidence fallback | Независимый Windows adapter; идеи checkpoint/recovery и provider interfaces |
| Meetily upstream | `main`, `a2cb62e827da7ef59f65064c97233efb2313878e` | На 171 commit впереди локального; основные риски активного pipeline ещё существуют | Не переносить desktop/runtime целиком; Pro не используется |
| `meetscribe` | `restore/P031-migration-snapshot`, `c95bb526c0069a11a1467e29c562c68228943bce`, только `.agent/` | Windows unsupported, capture dependency shims, whole-file RAM, GPL | Код не переносить; самостоятельно реализовать bounded processing |
| `minutes` | `restore/P033-migration-snapshot`, `4c4ac93c99030b7ec301f93e242ea8eed265497f` | Native Windows system capture отсутствует; non-whisper STT placeholder; идеи WAV sidecar | Код не переносить, собственные контракты/экспорт |
| `amazon-transcribe-live-meeting-assistant` | `restore/P028-migration-snapshot`, `06105da6ad3d0a5ca4c10773aeaa2039568c4238`, только `.agent/` | AWS/cloud инфраструктура, WebAudio streaming, нет локального Windows durable pipeline; file-level licenses расходятся | Не переносить инфраструктуру; учесть teardown/streaming pitfalls |
| `ai-meeting-secretary` | `main`, `0268c2bd4cebfbba98e0001b5c75bd70b3e52cd9`, isolated clone clean | Пригодны UI primitives/design; demo fallback, keys browser storage, API/schema/retry defects | Перенести только React presentation; backend и клиент заменить |

## Фактический перенос

Выборочно перенесены `cn.ts`, `Card.tsx` и адаптированы `Button`, `Badge`, `Tabs`, `LogoMark`, palette/CSS/Tailwind из **собственного** ai-meeting-secretary. Новый workspace, typed HTTP/SSE client и business flow реализованы заново. Точная карта new→donor→treatment: [frontend/REUSE.md](../frontend/REUSE.md). MainPage целиком, demo fixtures/KPI, localStorage keys, old hooks, heavy local models, PostgreSQL/Redis/Celery и чужие provider clients не перенесены.

Из Meetily/Minutes/meetscribe/AWS нет прямого копирования исходников. Архитектурные идеи incremental file storage, provider boundary и source-linked summaries реализованы самостоятельно. Закрытый Meetily Pro не изучался и не используется. MIT/GPL лицензии разных доноров не присваиваются всему Secretary; собственный GitHub-донор не имеет окончательной публичной лицензии, и перенос выполнен по явному разрешению владельца.

## Подтверждённые дефекты и новый контракт

| В старом коде | В Secretary | Приёмка |
|---|---|---|
| API unavailable → successful demo | Реальный error/waiting_config, без production mocks | API-тесты + настоящий импорт через UI без ключа |
| null timestamp → 0 | Nullable ms и явная timing_precision | Provider/schema tests, UI approximate/unknown labels |
| confidence 0..1 отображается как уже проценты | Единый nullable 0..1 контракт; no invented 0.85 | OpenAPI/types + provider tests |
| Готовое tasks=[] → 404 | 200 []; ещё не готово отдельно 202 | API empty-summary test |
| Shared API key в localStorage | Polza key в backend `.env`; UI memory-only CSRF | Public config/token/security tests + static client storage inspection; browser storage runtime не проверялся |
| Nonatomic jobs + новая transcript на retry | SQLite atomic claim, stable IDs/unique artifacts, selected-stage retry | Worker crash/restart/concurrent claim tests |
| Raw transcript перезаписан cleanup | Исходные сегменты и readable_transcript раздельно | Summary version/evidence tests |
| Speaker/channel/name смешаны | Каналы отдельно; labels scoped to chunk, неизвестное имя null | Capture/provider/schema tests |
| Unknown cost = missing accounting | Estimate/reservation/confirmed/unknown distinct; spend gate | Budget/receipt/error tests |

## Подробные таблицы и пределы

Каждый источник содержит обязательную таблицу `источник → файл/модуль → состояние → решение → причина → проверка переноса`, active call paths, собственный file inventory, test coverage и licenses/unknowns:

- [Meetily local + upstream](audit/MEETILY.md).
- [meetscribe, Minutes, AWS LMA](audit/OTHER_DONORS.md).
- [ai-meeting-secretary](audit/AI_MEETING_SECRETARY.md).

Аудит охватывает собственный код по подсистемам, исключая зависимости, окружения, сборки, модели и большие медиа. AST/static inspection донора не объявляется runtime PASS. Полные donor test suites не запускались: они требуют тяжёлых/runtime/cloud зависимостей и могли бы изменить их состояние. Старые отчёты использовались только как вопросы; изменившиеся выводы сопоставлены с текущими active sources. Проверки переноса относятся к новому Secretary, точные результаты находятся в [VALIDATION.md](VALIDATION.md).
