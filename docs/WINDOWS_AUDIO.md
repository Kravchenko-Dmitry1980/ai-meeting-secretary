# Запись Windows: реализованный контракт и проверка

`backend/secretary/infrastructure/audio.py` реализует явный `AudioCapture.start`, `stop`, `state`, `close`, read-only `list_devices` и `recover`. Запись создаётся только после действия пользователя в UI/API. Инициализация приложения, список устройств, импорт и recovery не открывают recording stream. Отсутствие Polza API-ключа не влияет на аудиозахват.

## Native API и устройства

Используется установленный в Secretary `.venv` **PyAudioWPatch 0.2.12.8**. Официальные первичные источники проверены 2026-10-01: [PyAudioWPatch README](https://github.com/s0d3s/PyAudioWPatch), [пример WASAPI loopback](https://github.com/s0d3s/PyAudioWPatch/blob/master/examples/pawp_record_wasapi_loopback.py). WASAPI loopback endpoints появляются как отдельные input devices; backend выбирает именно loopback analogue, а не обычный output endpoint. В коде используются документированные `get_host_api_info_by_type(paWASAPI)`, `get_device_info_generator`, `get_default_wasapi_loopback`, `open(... input=True, input_device_index=..., start=False)` и явные stream lifecycle методы.

IDs `wasapi:<index>` действуют для текущего устройства/сессии; после изменения оборудования пользователь обновляет список и выбирает устройство заново. Проверяется соответствие выбранного ID типу microphone/system. Каналы сохраняются **раздельно** в native PCM16 WAV с default sample rate/channels каждого устройства. Это канал источника, не установленный человек и не diarization. Одновременный sample-perfect hardware clock sync не заявляется; стартовые смещения каналов измеряются монотонными часами, дальнейшие offsets вычисляются из количества frames.

На этой машине read-only enumeration реально вернула 4 WASAPI input devices: 2 `Микрофон (Realtek(R) Audio)`, `Наушники (Realtek(R) Audio) [Loopback]`, `Динамики (Realtek(R) Audio) [Loopback]`. Default input и loopback отмечены. Ни один native recording stream в ходе этой проверки не открывался.

## Сохранение и устойчивость

Native master chunks: `data/audio/<meeting_id>/microphone_000000.wav`, `system_000000.wav`, и далее по sequence каждого канала. В процессе запись идёт в `.wav.part`. Полный chunk/последняя часть закрывается, flush/fsync завершаются, `os.replace` публикует WAV, SHA256 и frame offsets записываются в атомарный `capture.json`, затем вызывается application callback. Исходники Meetily не скопированы; адаптированы идеи progressive checkpoints, manifest recovery и provider boundary.

Manifest schema 1 хранит meeting ID, UTC timestamps, status/error, per-channel format/start offset, `chunks[]`, `in_progress{}`, recovery notes. Chunk receipt содержит `path,sequence,channel,offset_ms,duration_ms,sha256,start_frame,frames,sample_rate,channels`. Стабильная пара `(sequence,channel)` поддерживает повторную регистрацию в SQLite без duplicates. Callback failure сохраняет `delivery_error`; последующие аудиоданные продолжают записываться. Приложение повторяет регистрацию receipts при stop/restart.

PortAudio callbacks только проверяют формат/status и выполняют nonblocking enqueue. В callback нет дисковых операций, SQLite, HTTP, облачных SDK или расшифровки. Общая очередь ограничена 256 блоками; обычный 1024-frame stereo PCM16 block даёт около 1 MiB очереди. Callback большего чем 4096 frames отвергается; максимум при 8 каналах — 16 MiB PCM backlog. Полное аудио в RAM не накапливается. Disk thread пишет файл постепенно; осмысленные cloud jobs создаются отдельно application layer. Ошибка/overflow приводит к явной остановке с сохранением уже записанного, не к скрытому выбрасыванию старых данных.

Остановка закрывает/terminates только свои streams/manager и joins свой non-daemon writer thread. Повторное stop безопасно. После 3 часов capture автоматически завершается. Необязательный `on_finish(snapshot)` вызывается после закрытия всех native handles/WAV; он дополнительно получает durable `chunks[]` для синхронизации SQLite даже после failed receipt callback. Completion callback выполняет только DB-операции, не вызывает capture/recovery/stop или сеть из writer thread. Вторую запись в ту же встречу с существующим manifest отклоняем, чтобы не перезаписать источник; создайте новую встречу. При зависшем аудиодрайвере/диске stop возвращает явную ошибку после 30s и не запускает второй writer поверх первого; принудительное завершение чужих процессов отсутствует.

Облачная подготовка — отдельная стадия: native masters сохраняются, worker делает disk derivative PCM16 **16 kHz mono**, и ограничивает file chunks по Polza envelope. Native 48 kHz stereo120s WAV нельзя напрямую считать подходящим для 14 MB JSON/data URL body. Путь облачных derivative и точное ограничение находятся в `application/worker.py` и `docs/POLZA_CONTRACT.md`; raw masters не заменяются.

## Recovery `.part`

1. Успешно закрытые WAV проверяются по SHA256 и receipts повторно передаются repository.
2. При crash между rename и manifest commit существующий WAV проверяется по формату из `in_progress`; восстанавливается receipt.
3. Для `.part` автоматически принимается только стандартный 44-byte RIFF PCM16 header с rate/channels из manifest. Реальное число полных frames берётся из размера файла; stale header duration не используется. Данные копируются ограниченными blocks в новый исправленный WAV через temporary file/atomic rename. Неполный trailing frame не включается. **Исходный `.part` сохраняется полностью**.
4. Неполный/неподтверждённый header не преобразуется в fake successful audio chunk; исходный файл и descriptor остаются для проверки. Recovery не открывает устройства, не вызывает Polza и не удаляет raw audio.
5. Interrupted status становится `recovered`, original crash отмечается error/note. Повторный recovery не создаёт новый sequence или duplicate receipt.

Flush/fsync active PCM выполняется приблизительно раз в секунду и на закрытии. При аварийном завершении данные ещё находившиеся в очереди/OS buffer могут отсутствовать; exactly-once или нулевая потеря при power failure не обещаются.

## Выполненные проверки

Команда: `.\.venv\Scripts\python.exe -m pytest tests/test_audio.py -q --basetemp data\test-audio-tmp` — **15 passed**. Проверены synthetic callbacks двух каналов, точные frame boundaries/offsets, atomic chunks/manifest, bounded queue и overflow error, unavailable/invalid device contracts, отсутствие recording при list, stop/join/cleanup, automatic duration cap, failed second-device start с сохранением PCM первого, hash validation, сохранение `.part`, stale-header repair/rename recovery/idempotency, продолжение disk persistence после application callback failure. Независимый code review выявил и исправил два race/coverage дефекта: inactive канал теперь обнаруживается даже при постоянном backlog второго; stop во время completion callback сохраняет terminal status. Оба сценария покрыты regression tests.

600 секунд synthetic PCM, 9.6 MB на диске, 30 chunks: `tracemalloc` peak меньше 3 MiB; это Python writer-memory check, не замер общего RAM/CPU native аудиодрайвера и не 3-часовой аппаратный benchmark.

**Не проверено аппаратно:** открытие real microphone и real WASAPI loopback, слышимость обоих channels, permissions, disconnect/reconnect поведения конкретного Realtek драйвера, 3-часовой native run. Для короткого приёмочного теста пользователь выбирает два реальных устройства в UI, явно нажимает «Записать», произносит фразу в микрофон и включает тестовый системный звук, затем завершает запись; оба WAV нужно прослушать отдельно. File/synthetic tests не являются доказательством этого сценария.

## Лицензии

PyAudioWPatch: [Apache-2.0, Copyright (c) 2022 S0D3S](https://github.com/s0d3s/PyAudioWPatch/blob/master/LICENSE.txt); installed notice сохранён в `docs/licenses/python/PyAudioWPatch/LICENSE.txt`. Встроенный PortAudio имеет самостоятельный разрешительный MIT-style notice: [vendored PortAudio LICENSE.txt](https://github.com/s0d3s/PyAudioWPatch/blob/master/portaudio_v19/LICENSE.txt), blob `e0ac4e8a0c0188cece03c15e956b800f34e1c449`, полный текст сохранён в `docs/licenses/portaudio/LICENSE.txt`. Метаданные wheel не заменяют лицензию native library; двоичные библиотеки отдельно не скачивались и не перераспределялись вне изолированной зависимости проекта.
