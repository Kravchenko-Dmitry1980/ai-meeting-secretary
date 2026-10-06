# Проверка native-запуска с observer Windows Job

2026-10-04. **Фактический запуск остался FAIL; диагностика observer и сохранность synthetic fixtures подтверждены.** Полный R4 не завершён, Team NOT_READY, goal ACTIVE.

## Фактический результат

Root выполнил один заранее просмотренный запуск: chunk `e389a3`, exit 1, harness 2.203 s. [Evidence](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/evidence.json) сохраняет `unknown_owned_job_descendant`, sticky FAIL и `experiment_completed=false`; [root log](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/root/native-observed.log) не заменяет этот отрицательный результат успехом collector.

Два собственных CLI seed завершились exit 0. На candidate появился дополнительный current Job member. Единственный observer удержал его process HANDLE, время создания и текущие path-file факты с безопасным именем `conhost.exe`; terminal факт того же HANDLE — alive=false / exit 0. [Observer receipt](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/job-observer-receipt.json): `DIAGNOSTIC_COMPLETE`, 14 records, 3 retained lifetimes, 0.093 s, empty failures, cleanup_complete=true. Неизвестный member не прекращался адресно и не включён в allowlist.

Candidate принудительно остановлен через собственный retained Popen HANDLE, exit 1; [candidate log](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/candidate-normalpath-nowrite.log) пуст. Поэтому actual SQL startup denial **NOT_PROVEN**; ready/startup success тоже не доказан. HTTP и authenticated GET — **NOT_RUN**, HTTP actions=0. Имя текущего member не устанавливает ancestry, mapped image, OS signature, source correspondence или authority: эти границы UNKNOWN / NOT_EVALUATED. Три неизвестных member предыдущих попыток задним числом не типизированы.

## Сохранность и cleanup

Полное сравнение сохранённых [baseline](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/all-four-closed-baseline.json) и [afterstop](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/all-four-closed-afterstop.json) подтвердило одинаковые state, полные bytes/length/FileID всех четырёх synthetic roles. Sidecars отсутствуют; actual root-only Job gate пройден до immutable SQL readback под удержанной NoWrite custody. Native сохраняет 42 business tables, 390 triggers, head 0, events 0, future grants 0; schema/metabodies/business hidden-rowids входят в сравнение. Config, binary и пустой asset inventory неизменны. Три marker DB не являются реальными Secretary/Team/Billing authorities; реальные вложения не квалифицированы.

Все known Popen children/readers остановлены, observer handles очищены, actual final Job root-only. `final_cleanup_complete=true` записан **до** глобального `ExitStack.close()`: этот receipt не доказывает результат закрытия каждого NoWrite Win32 file lease. Фактический exit root-процесса — отдельное наблюдение, не per-handle CloseHandle proof.

[Before/after 297 pins](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/root/after-native-297.json) совпадают; read-only rehash текущих named 297 files также совпал. Прежние 17 файлов failed `e54729f1f799497aa43b4499d1d20d21` сохранены неизменными по root snapshot и [независимому actual review](../.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/review/actual-native-observation-review.md). Старый e547 запуск был preflight FAIL `operator_custody_invalid`, native0; исправление выполнено новым GUID через фактическую private-directory preparation, без ремонта старого ACL/receipt.

## Точные SHA256

Пути относятся к `.runtime/team-rollout/r4-native-observed-2c660c1fb0664760b0edc98a4afb64fa/`.

| Артефакт | SHA256 |
| --- | --- |
| `evidence.json` | `e774b89905e13bff7ed37a8d23753830601531da16b03a3204b3196e773e414f` |
| `job-observer-receipt.json` | `1a925df8e39e71288e34f20a022cb1d1292689d1bf3e9d31536512aa99efd138` |
| `all-four-closed-baseline.json` | `9d9e9efa0e00be02a6076a992ef1919effa7d3cb6d151f19a0b971c127e95b82` |
| `all-four-closed-afterstop.json` | `09163a3a2407306c0d1e3f336a9be46175bed71f101048534e024bad9651d699` |
| `probe_native_observed_nowrite.py` | `5e9b7fe6e709c17eae7dbe958cb6103af0a2ac565e51a9073722394e618ed7e4` |
| `review-pins.json` | `49f8697ea6b6ae492896176b19107b1a62a5004f6d4b6b9263fa306365af8404` |

## Ограничения и следующий шаг

Текущий helper **уже** использует `CREATE_NO_WINDOW` на строках 348–349. Pinned native SHA `e485792c33f537124fb187658a84099a74ab0f1b948a44dee9049fede1ad66b6` проверен; PE subsystem=3. Поэтому «добавить no-window flag» не является найденным исправлением, а сам флаг не доказывает отсутствие OS sidecar. Предлагается следующая отдельная узкая карточка: установить фактическую console dependency/launch contract; возможное synthetic сравнение с отдельным `DETACHED_PROCESS` требует нового reviewed contract. Пятый native запуск здесь не разрешён; нет allowlist expansion, RW fallback или guard relaxation.

79 observer PASS — отдельная ранее завершённая проверка; 4725 backend PASS — исторический full run, не свежая проверка этой native карточки. Для отчёта новых tests/native/process/PID/provider действий не выполнялось.

Browser-сертификат пока не удалось проверить человеку; fixture остановлена, T9 NOT_RUN. Full R4, activation, fresh owner/provider grants, common predicate/final decision, independent native head/lineage, MAX, ручная проверка, телефон, HTTPS и 24h работа остаются незакрытыми. В native receipt owner/provider/paid/Git actions=0; бюджет Polza 3000 ₽/месяц не расходован этой карточкой.
