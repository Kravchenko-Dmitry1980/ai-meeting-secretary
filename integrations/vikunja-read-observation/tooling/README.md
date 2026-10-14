# Offline egress candidate — исходники для сохранения работы

Статус: **ORIGINAL_HOST_BOUND_NOT_LAUNCHABLE**.

В `source-only/` лежат byte-identical копии двух Python helpers и двух pure test
sources с суффиксом `.py.txt`. Расширение выбрано для архивного хранения: эти файлы
не являются новым исполняемым инструментом и не входят в pytest discovery.
Не переименовывать и не запускать `main` в рамках публикации проекта.

`manifest.json` фиксирует original/export SHA256 и длины. Абсолютные пути
исторического проекта и pins внутри исходников оставлены как часть истории;
их наличие не означает доступность входов на новом компьютере. Секреты, базы,
owner content, receipts, tools, caches и список module paths не копировались.

Candidate реализует deny-all проверку Go environment до child launch. Его
предыдущие восемь pure checks были зафиксированы в существующем dependency
отчёте. При этом экспорт не запускает Go, helper main, SQL, native API или
внешние запросы и не повторяет эти тесты.

Для отдельного будущего переноса:

1. Определить явные inputs и sandbox scratch binding; убрать зависимость от
   historical absolute paths без снятия проверок identity, pins и границ I/O.
2. Сохранить точное сравнение набора ключей/значений offline environment и отказ
   `online=True` до запуска дочернего процесса. Решение владельца — offline-only.
3. Перенести два pure test sources и выполнить их отдельно без Go/helper main.
4. Получить независимый review, новые exact pins, environment success contract
   и launch evidence. Старые receipts нельзя переносить как текущий PASS.

Пока эти шаги не завершены, рабочий код сохранён для разработки, но
dependency acquisition и native profile остаются **NOT QUALIFIED**.
