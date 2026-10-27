# ADR: общий ASR core, отдельные среды и единственный владелец задания

Дата: 2026-10-26. Статус: принято для model-free подготовки; model runtime и SERVER cutover не активированы.

## Контекст и область решения

Основание — CURSOR_SECRETARY_IMPLEMENTATION_PROMPT v2, Handoff и Strategy v2 от 26 октября,
а также актуальный `docs/SECRETARY_CURRENT_STATE_2026-10-26.md`. Выполняется разрешённая
подготовка и локальная интеграция. Пауза inference из Handoff §11.2/§15.3 действует.
Наличие исходников или синтетического PASS не является допуском моделей/production.

Текущая рабочая машина фактически является ноутбуком. При принятии ADR desktop
source/evidence bundle отсутствовал; начальный аудит: [runtime-audit.md](runtime-audit.md).
Позднее 26.10.2026 bundle получен и проверен, а полный model-free gate адаптированной
копии прошёл 198 Python + 6 PowerShell tests; актуальные доказательства:
[full-package-test-runtime.md](full-package-test-runtime.md). Доступный старый
NeuroHR Core действительно отбрасывает `speaker_turns`; его изменение и live deployment
не выполняются этой задачей. Архитектура SERVER не выдумывается по названию проекта.

## Решение

1. Общий pinned package владеет normalization, diarization, RNNT, CTC alignment,
   assembly/bridge и проверками артефактов. Выбранный профиль — `nemotron-ctc`.
   CTC привязывает выбранный текст; selectors, второе распознавание готовой встречи
   и новые обязательные экспериментальные модели не добавляются.
2. RNNT/CTC, Nemotron и существующий ECAPA имеют отдельные среды. Web-process не
   импортирует Torch и не загружает веса. Приложение не копирует экспериментальный
   pipeline из Markdown. Переданный документ не заменяет исходный package/locks.
3. До отдельного cutover очередь и пользовательский job принадлежат Secretary.
   `local_asr_executions` — метаданные существующей таблицы `jobs`, не новый broker
   и не второй polling worker. SERVER получает адаптер к своему фактическому
   механизму после аудита и отдельной интеграционной квалификации.
4. Приложение владеет встречами, person profiles, roster, ручными подтверждениями,
   итогами и задачами. Анонимный кластер уникален в пределах run и не является person ID.
5. Существующий Polza route и конфигурация не меняются. Сбой локального admission
   никогда не переводит задание в облако. Повтор неопределённого paid POST запрещён.

## Контракты и владелец данных

| Граница | Вход | Выход / владелец |
| --- | --- | --- |
| Core → приложение | `neurohr.local-asr.v2`, исходный hash | Исходный DTO сохраняется полностью |
| Sidecar | `secretary.local-asr-artifacts.v1` | Выбранный текст, words, raw turns, source mapping, provenance |
| Import | Проверенный сервером media hash, meeting/version, operation ID | Immutable manifest/revision, идемпотентная квитанция |
| Manual review | Показанный набор, исключения, revision, участник, явное подтверждение | Отдельная append-only история решений |
| Job | Серверный frozen execution plan | Existing job + lease/attempt/fence/checkpoints |
| Resource | Общий physical GPU UUID, provisioned lock root, свежие ресурсы | Admission либо waiting/blocked, без cloud fallback |

Схема `neurohr.local-asr.v2` и внешний service endpoint `POST /v2/transcriptions`
не переименовываются. Secretary является потребителем артефактов и добавляет свой
локальный API `/api/v1/meetings/{id}/local-asr`. Он не выдаёт себя за model service.

## Поведение готовой встречи

Новый анализ сравнивает сохранённые source segments, текст, channel и времена с
текущей БД внутри transaction. Повторный импорт не создаёт вторую версию артефакта.
Текст, transcript route, оплаченные checkpoints/receipts, summaries, task assignments
и существующие attribution decisions не переписываются. Ручное имя анализа также
не запускает LLM и не переназначает задачи из ранее оплаченных итогов.

Saved-text alignment имеет самостоятельный интерфейс. Совместимость Whisper-текста
с pinned CTC не объявлена: неподтверждённые токены/окна или повреждённый результат
возвращают empty words + явную диагностику с сохранёнными исходными segment times.

## Текущая модель доступа

Secretary остаётся однопользовательским loopback-приложением с существующими
Host/Origin/CSRF проверками. `local-owner` определяется сервером. Scope/run/meeting
проверяются на чтении, экспорте и изменениях. API не принимает токены model service,
произвольные filesystem paths, owner identity, deployment permission или execution plan.
Это не реализация tenant auth SERVER. Соответствующий адаптер остаётся отдельной границей.

## Trade-offs и ограничения

- SQLite sidecar обеспечивает атомарность и сохранность legacy business data без
  смены базы. Manifest хранится в SQLite как immutable JSON; это не набор внешних
  файлов, требующий отдельного двухфазного commit.
- Полный import ограничен 16 MiB; долгий inference не выполняется в HTTP request.
  Для большей evidence-упаковки потребуется отдельный ограниченный transport.
- Канонический вход для записи из нескольких файлов/каналов нельзя подменить
  hash списка chunks. До квалификации materialization такого input импорт блокируется.
- Общий lock защищает лишь потребителей, использующих согласованный device/root.
  Он не резервирует память у чужих процессов. Наличие файла lock не доказывает
  интеграцию с SERVER. Его adapter и native containment ещё не квалифицированы.
- Автоматические реальные имена по новым кластерам выключены: подходящей cluster
  calibration нет. Ручное именование возможно без `proposed`; неоднозначные реплики
  исключаются. Veche не используется для voice identification.
- Внешний source pin исправляется только при одновременной проверке официальных
  metadata, canonical manifest и локального веса. Сейчас последнее отсутствует.

## Приёмка и откат

Десять обязательных model-free рисков и evidence перечислены в итоговом
[implementation-report.md](implementation-report.md). Реальные ASR quality, hardware
admission на desktop, длительное model execution, SERVER auth/storage/load и human
listening не подменяются синтетическими проверками.

Откат и перенос описаны в [rollback.md](rollback.md) и
[transfer-and-qualification.md](transfer-and-qualification.md). Нельзя делать `git reset`
или восстанавливать целиком dirty файлы: перед этой задачей уже существовали изменения.
