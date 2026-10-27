# Изолированная первая проба runtime

`scripts/run_local_asr_pilot.py` собирает первое аппаратное доказательство отдельно
от очереди приложения. Он не создаёт jobs/meetings, не открывает live DB, не включает
`SECRETARY_LOCAL_ASR_PROFILE` и не пишет production qualification.

Вход — локальный JSON `secretary.local-asr.bootstrap-authority.v1`, чей SHA-256
оператор передаёт отдельно. Его обязательные поля:

- `owner_authorization_id`: ссылка на прямое разрешение владельца этой пробы;
- `purpose`: `first_bounded_hardware_pilot`;
- `profile`: существующий `PortableRuntimeProfile`, обязательно `enabled=false`,
  `qualification_file=null`, RNNT/CTC CPU и Nemotron CUDA с физическим GPU UUID;
- `input` и `input_provenance`: `{path, sha256}`;
- `launcher_python`: `{path, sha256}` текущего интерпретатора приложения;
- `pilot_runner_sha256`: SHA-256 установленного runner;
- `environment_inventories`: pinned receipts `launcher`, `asr-cpu`, `nemotron`.

Параметры ограничены: `max_input_seconds=30`, `max_attempts=1`,
`deadline_seconds<=1800`, `stage_timeout_seconds<=600`,
`production_activation=false`. Допустим только полный mono PCM16/16kHz WAV
до 30 секунд и до 2 MiB. Исходник и его отдельная копия держатся открытыми
на Windows без write/delete sharing до окончания процессов.

Environment inventory создаётся `config/local-asr/model-runtime/environments/capture_inventory.py`.
Проверяется полный набор файлов env и базового Python, включая `.pyc`, `.pth`, DLL,
stdlib, интерпретаторы и lock receipt. Дополнительный либо изменённый файл блокирует
запуск. Источник проверяется по всей delivery closure; модели — по обязательному
manifest и разрешённым исходным metadata. Неизвестные альтернативные checkpoint
файлы запрещены. Чтение authority/inventory проверяет digest тех же bytes, которые
передаёт JSON parser.
Captured `pyvenv.cfg` должен содержать единственный absolute `home`, разрешающийся
в exact plain `base_runtime.root`, а `home/python.exe` — в pinned base interpreter.
Mapping проверяется до и после полного hash scan. Для текущего launcher также
сверяются actual `sys._base_executable` и `sys.base_prefix`. Существующая uv junction
не изменяется; retarget запрещён. Эти проверки выявляют дрейф, но не означают
неизменяемость уже загруженного Python на уровне ОС.

`verify_inventory(plan)` возвращает только integrity evidence и всегда
`qualification_granted=false`. Обычный `qualification()` по-прежнему отдельно
требует `enabled` и прежние qualification proof flags. Bootstrap не подделывает
`model_runtime_verified` или `environment_inventory_verified`.

Контроллер запускается ровно один раз внутри Windows Job. Каждая стадия получает
от родителя одноразовый permit после свежего admission, а затем подтверждает
очистку собственного дерева. Сохраняются прежние пределы: RAM admission 6656 MiB,
commit admission 9216 MiB, aggregate Job commit 8192 MiB, monitored owned RSS
4608 MiB. Во время выполнения отдельный watchdog останавливает принадлежащее
дерево при host free RAM ниже 2048 MiB или free commit ниже 1024 MiB.

VRAM admission 7168 MiB применяется только к этой ограниченной пробе. Он не
считается измеренным универсальным минимумом Nemotron. Во время занятого GPU
watchdog собирает минимум свободной VRAM, не повторяя admission threshold на
каждом poll. Недоступная или устаревшая telemetry блокирует выполнение.

Shared root этой пробы следует явно задать как
`D:\AI\Projects\Active\Secretary\.runtime\model-pilot-shared-resources`.
Это координация участвующих пилотных процессов; она не доказывает подключение
прочих продуктов к единому live scheduler. Неизвестная очистка сохраняет
durable quarantine; runner её не снимает автоматически.

Donor CLI принимает duration profiles только 1800/9000/14400, поэтому command
использует минимальный 1800. Более строгий предел 30 секунд применяется независимо
к входу и к normalized.wav до допуска дальнейших моделей. Исходный пакет не
редактируется. Приватные HF/Torch/Numba/Librosa caches находятся внутри attempt.

Контроллер сохраняет bounded stdout/stderr только при literal
`retain_stage_diagnostics=true`. Рабочий executor этот флаг не передаёт.
`DEV_ASR_TRACEBACK` остаётся 0. Логи находятся в `control/stage-N-diagnostic.json`,
их hashes включены в native receipt. Публичная ошибка содержит только код.

Пример команды после независимой проверки authority (здесь placeholders):

```powershell
.venv\Scripts\python.exe -B scripts/run_local_asr_pilot.py `
  --authority <ABSOLUTE_AUTHORITY_JSON> `
  --authority-sha256 <REVIEWED_SHA256> `
  --output <NEW_ABSOLUTE_DIRECTORY_UNDER_PROJECT_RUNTIME>
```

Команда не исполнялась при разработке runner. Synthetic проверки выполняются так:

```powershell
.venv\Scripts\python.exe -B scripts/run_local_asr_tests.py tests/test_local_asr_pilot.py tests/test_local_asr_runtime.py tests/test_local_asr_executor.py -q
```

SIGINT/SIGBREAK запрашивает отмену; родитель ждёт native cleanup. Автоматического
retry нет, даже несмотря на `max_attempts=2` в совместимом runtime plan template:
bootstrap envelope допускает один controller dispatch. После остановки attempt
и все partial artifacts сохраняются. Повтор требует нового явно выбранного attempt.

Успех на коротком WAV не доказывает качество, пригодность длинных встреч,
production qualification или согласование владельцем услышанного результата.
Tokenizer compatibility остаётся отдельным требованием adapter validation.
Если inference и проверки целостности прошли, но tokenizer пока `unverified`,
runner возвращает exit code 2 и `inference_succeeded_adapter_blocked` с причиной
`package_tokenizer_not_verified`. Raw artifacts и post-inference inventory сохранены;
это позволяет проверить tokenizer и выполнить отдельную проверку артефактов без
повторного запуска моделей. Exit code 0 означает также успешный import adapter;
exit code 1 — ошибку. Ни один из статусов не разрешает production activation.
