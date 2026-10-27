# Откат и сохранность данных

Дата: 2026-10-26. В текущей фазе live БД и действующий runtime не обновлялись.

## До первого запуска обновлённого приложения с рабочей БД

1. Зафиксировать отсутствие активной записи/paid send и ownership работающего
   приложения. Не завершать чужие процессы. Если безопасный backup требует остановки,
   это отдельное согласованное обслуживание, не действие текущей model-free фазы.
2. Создать согласованный SQLite backup через штатную backup процедуру проекта с
   учётом WAL. Простое копирование `.sqlite3` отдельно от активного WAL недопустимо.
3. Проверить integrity_check/foreign_key_check копии; сохранить fingerprints исходных
   segments/route/summaries/tasks/manual decisions, usage и receipts. Не помещать
   тексты/аудио/голоса/секреты в Git или публичный report.
4. Сначала выполнить upgrade/restore на изолированной копии. Новые local_asr tables
   должны сохраняться как evidence/no-replay в restore quarantine, без автоматического
   запуска старых jobs. После этого отдельно разрешается переключение runtime.

## Source rollback текущей задачи

В workspace до начала задачи уже были изменения. Их нельзя удалять `git reset`,
`git checkout --`, полным восстановлением файла или отменой всех diff.
Исходные copies затронутых dirty файлов сохранены в игнорируемом каталоге
`.runtime/local-asr-preparation-20261026`; это справка для трёхстороннего ручного
сравнения, а не команда заменить текущие файлы.

Локальные добавления изолированы в новых `local_asr*.py`, `LocalAsrReview.tsx`,
frontend localAsr modules, tests, config/local-asr и docs/local-asr. В прежних файлах
добавлены startup/schema/router wiring, ограниченный body limit для import,
fail-closed local stage, безопасная отмена, component mount и restore compatibility.
Для source rollback удалить только эти additions после сравнения с текущими
параллельными изменениями. Database legacy business columns/rows не откатывать.

## После первого реального использования

Новые immutable manifests и manual review events являются пользовательскими
данными; их нельзя удалять ради отката к старому UI. Сохранить full verified backup
и export runs/reviews. Отключить дальнейшее исполнение локального adapter, оставить
Polza route и старые оплаченные результаты. Существующий runtime default на Polza
не менялся, поэтому rollback не требует повторной транскрипции или новых итогов.

Restore никогда не снимает неопределённость paid POST и GPU quarantine. Очистка
quarantine требует доказанного завершения собственного дерева процессов и отдельной
операторской процедуры. Новая версия не получает право inference из одного restore.

## Stop conditions

Hash/provenance mismatch; недоступный clean rebuild; admission refusal; потеря
tokens/turns; изменение ручных решений; duplicate job/stale publication; неизвестный
paid result; несовместимый backup catalogue. При любом из этих событий новый route
остаётся выключенным, evidence сохраняются, независимые подготовительные работы
можно продолжать. Пороги и владельцы чужих процессов не изменяются ради PASS.
