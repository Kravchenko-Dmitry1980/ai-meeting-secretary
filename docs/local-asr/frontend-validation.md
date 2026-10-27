# Проверка интерфейса локального анализа — 26 октября 2026

**PASS: model-free интерфейс на синтетических данных.** Проверены реальный
`LocalAsrReview`, общие стили приложения, `create_app`, новые HTTP routes и SQLite.
Работающая установка, её frontend/dist и пользовательские данные не использовались.
Модельное качество, реальные голоса, человеческое прослушивание и SERVER deployment
этой проверкой не квалифицированы.

## Стенд и ограничения

- Изолированный каталог: `.runtime/local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4`.
- Vite `configFile:false`, выход только в `frontend/dist` **внутри этого каталога**.
  Прокси рабочего Vite к порту 8765 не подключался.
- Реальный FastAPI запущен на собственном loopback-порту 63839 с отдельными SQLite,
  `run_worker=False`, `outbound_enabled=False`, пустым ключом, `_env_file=None`.
  Исходящие `httpx` send и provider factory дополнительно заменены запретом.
- Вход: 5 секунд синтетической тишины PCM16 mono 16 kHz и
  `tests/fixtures/local_asr/synthetic_run.json`. Сохранённые слова не утверждают
  наличие речи в WAV; это проверка контрактов и ручного workflow.
- Браузер: отдельный Chrome context; desktop 1366×1000 и mobile emulation 390×844.
  Стенд остановлен через его собственную exec-сессию; TCP listener отсутствует.

## Выполненные действия и наблюдения

1. Открыл сохранённый анализ: пять реплик, исходный текст, три аудиопримера,
   шесть независимых speaker turns. Смешанный и неизвестный голоса отображаются
   явно. В таблице одновременно сохранены два интервала 3000–3800 ms.
2. Убрал отметку с реплики #2, выбрал синтетического участника, выставил обе
   явные галочки проверки и нажал подтверждение двух выбранных реплик.
   Реальный `POST .../cluster-reviews` вернул HTTP 200. Только ordinal 0 и 2
   стали `confirmed/manual_cluster`; ordinal 1 — `excluded/manual_segment`.
3. Открыл отдельную проверку реплики #1 и сохранил «Оставить без имени».
   `POST .../segment-reviews` вернул HTTP 200. После нажатия «Обновить анализы»
   ordinal 0 остался `excluded/manual_segment`, ordinal 1 сохранил исключение,
   ordinal 2 сохранил подтверждение. Review revision выросла до 2.
4. Проверил экспорт по ссылке интерфейса: HTTP 200,
   `Content-Disposition: attachment; filename="local-asr-analysis.json"`.
   Экспорт содержит неизменные слова, word spans, raw turns, неизвестность,
   смешанную метку и отдельные ручные решения.
5. В mobile emulation `innerWidth=390`, `scrollWidth=390`; горизонтального
   переполнения нет. Все шесть raw turns отрисованы, повторная групповая запись
   недоступна для уже проверенных реплик. После финальной перезагрузки console
   errors/warnings отсутствуют; API/media запросы направлены только своему
   loopback-стенду, ответы HTTP 200/206.

Нативное воспроизведение трёх WAV-фрагментов технически запущено. Галочки
прослушивания в этой автоматической проверке являются **синтетическим действием**,
а не фактом человеческой оценки голосов. Ссылки используют временные media
fragments и остановку по `timeupdate`; это просмотр исходного аудио, не физически
вырезанный WAV. В данном браузере остановка могла запаздывать примерно на
0,1–0,23 секунды, поэтому точность обрезки по аудиосэмплам не заявляется.

Во время browser smoke обнаружено и исправлено округление коротких интервалов
до одинаковых секунд: теперь показываются миллисекунды, например
`00:00.000–00:00.800`. Повторная сборка и проверки прошли после исправления.

## Доказательства

Внутри каталога стенда:

| Артефакт | Содержимое |
| --- | --- |
| `browser-assertions.json` | 17 выполненных browser assertions, все true; финальный JSON export |
| `export-after-cluster.json` | JSON после группового подтверждения и явного исключения |
| `browser-final-snapshot.txt` | Фактическое дерево доступности после обновления |
| `browser-network-summary.txt` | URLs, методы и статусы; без заголовков с токеном |
| `desktop-before.jpg` | Desktop: исходный текст, аудиопримеры, точный набор и raw turns |
| `mobile-after.jpg` | Mobile: сохранённые индивидуальные исключения и overlap |
| `evidence-integrity.json` | SHA-256 evidence, неизменность исходных segments/route/summary, отсутствие jobs/usage, один analysis run, SQLite integrity/FK, остановка стенда |

Скриншоты:

- [Desktop](../../.runtime/local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4/desktop-before.jpg)
- [Mobile](../../.runtime/local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4/mobile-after.jpg)

## Реально выполненные команды

Из корня проекта:

```powershell
node --experimental-strip-types --test frontend/tests/local-asr.test.mjs
node .runtime/local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4/build.mjs
.venv\Scripts\python.exe .runtime\local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4\server.py
.venv\Scripts\python.exe .runtime\local-asr-ui-53b29913-327c-45c3-b0b0-83b64c8662f4\verify_evidence.py
```

Из `frontend`:

```powershell
npm run typecheck
.\node_modules\.bin\eslint.cmd src/components/LocalAsrReview.tsx src/services/localAsr.ts src/types/localAsr.ts src/utils/localAsr.ts tests/local-asr.test.mjs
```

Результаты: focused frontend tests **7/7 PASS**, TypeScript **PASS**, scoped
ESLint **PASS**, isolated Vite build **PASS**, browser assertions **17/17 PASS**,
post-shutdown evidence checks **10/10 PASS**. Это scoped результаты; статус всего
репозитория определяется отдельным общим отчётом.

Для повторного просмотра сохранённого состояния достаточно запустить own
`server.py`: свободный порт печатается и записывается в `port.txt`. Для повторения
с чистого состояния сначала создаётся новый sibling-каталог `.runtime/local-asr-ui-<uuid>`
и копируются **только** `index.html`, `main.tsx`, `build.mjs`, `server.py`;
существующая база evidence не очищается и не переиспользуется как новая проверка.

Браузерный сценарий проверяет компонент в изоляции с реальным backend. Полный
запуск пользовательского App на рабочей установке, качество модели, реальные
ручные решения владельца и аппаратное прослушивание остаются отдельными этапами.
