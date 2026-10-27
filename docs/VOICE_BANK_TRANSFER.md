# Защищённый перенос voice bank

`scripts/voice_bank_transfer.py` создаёт переносимый AES-256-GCM пакет из
проверенных `VoiceStore` DPAPI envelopes. Отдельный случайный 256-bit key file
создаётся с Windows ACL только для текущего пользователя. Пакет не содержит ключ.
Нужны Windows, PowerShell 7.4+ (.NET 8+) и окружение приложения; новые зависимости не нужны.

Команды ниже являются шаблонами и сами по себе не выполнялись. Подставьте явно
выбранные абсолютные пути. Экспорт реальных голосовых профилей требует отдельного
разрешения владельца. Используйте согласованный холодный backup; оригинал не
меняется. Секретный key file храните и передавайте отдельно от пакета.

```powershell
# SOURCE_DATA — data directory внутри проверенного backup.
# PACKAGE, KEY и RECEIPT — новые файлы; их parent directories уже существуют.
# KEY находится вне каталога PACKAGE и вне SOURCE_DATA.
.\.venv\Scripts\python.exe -B scripts\voice_bank_transfer.py export `
  --data-dir '<SOURCE_DATA>' --package '<PACKAGE.svbank>' `
  --key-file '<SEPARATE_KEY.key>' --receipt '<EXPORT_RECEIPT.json>' `
  --source-backup-manifest-sha256 '<VERIFIED_BACKUP_MANIFEST_SHA256>'
```

На целевом компьютере ключ должен лежать на локальном NTFS volume, принадлежать
целевому Windows owner и иметь отключённое наследование ACL с доступом только
этому owner. При копировании ключа через USB эти свойства могут не сохраниться.
Следующая команда устанавливает их на **явно выбранной целевой копии ключа**;
значение ключа не читает и не выводит:

```powershell
# PowerShell 7; выполнить только для целевой копии ключа.
$KeyCopy = [IO.FileInfo]::new('<ABSOLUTE_TARGET_KEY_COPY>')
$OwnerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$KeyAcl = [Security.AccessControl.FileSecurity]::new()
$KeyAcl.SetAccessRuleProtection($true, $false)
$KeyAcl.SetOwner($OwnerSid)
$KeyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
  $OwnerSid, [Security.AccessControl.FileSystemRights]::FullControl,
  [Security.AccessControl.AccessControlType]::Allow))
[IO.FileSystemAclExtensions]::SetAccessControl($KeyCopy, $KeyAcl)
```

```powershell
.\.venv\Scripts\python.exe -B scripts\voice_bank_transfer.py verify `
  --package '<PACKAGE.svbank>' --key-file '<TARGET_KEY_COPY>' `
  --receipt '<NEW_VERIFY_RECEIPT.json>' `
  --source-backup-manifest-sha256 '<VERIFIED_BACKUP_MANIFEST_SHA256>'

# TARGET_DATA не существует. Импорт создаёт только voice/profiles/**/*.dpapi.
.\.venv\Scripts\python.exe -B scripts\voice_bank_transfer.py import `
  --package '<PACKAGE.svbank>' --key-file '<TARGET_KEY_COPY>' `
  --target-data '<NEW_ISOLATED_DATA>' --receipt '<NEW_IMPORT_RECEIPT.json>' `
  --source-backup-manifest-sha256 '<VERIFIED_BACKUP_MANIFEST_SHA256>'
```

`verify` проверяет AES-GCM authentication, формат transport и envelope identities.
`import` дополнительно перепаковывает исходные envelope bytes через CurrentUser
DPAPI целевого owner и проверяет результат каноническим `VoiceStore.read`.
Receipt содержит relative path, profile/enrollment IDs, material revision,
исходный/целевой ciphertext SHA-256 и одинаковый plaintext-envelope SHA-256.
Аудио, embeddings, plaintext envelopes и ключ в receipt/console не выводятся.

Импорт не открывает SQLite, не заменяет существующий voice tree и не активирует
приложение. После PASS, пока новая восстановленная копия приложения ещё не запущена:

1. Проверить совпадение backup manifest hash в export/import receipts и точное
   совпадение списка файлов, IDs/revisions и plaintext-envelope hashes.
2. Сверить ссылки БД: `(voice_enrollments.person_profile_id, storage_key, revision)`
   соответствует `(receipt.profile_id, enrollment_id, material_revision)`.
   Здесь нужен **storage_key**, а не `voice_enrollments.id`. Исторические строки
   и файлы могут не иметь текущей ссылки; их сохранить, а расхождения текущих ссылок
   исследовать до запуска. Семь строк истории не означают семь физических файлов.
3. Для каждого из четырёх allowlisted `voice/profiles/<profile>/<generation>.dpapi`
   проверить: hash файла в восстановленной `data` равен `source_ciphertext_sha256`,
   hash файла из isolated import равен `target_ciphertext_sha256`.
4. Сохранить исходный backup и заменить **только эти четыре файла** в новой
   восстановленной `data`. Не заменять весь каталог `voice`: он также содержит
   staging/capture metadata. Живая или ранее существовавшая рабочая папка запрещена.
5. Повторно прочитать все перенесённые материалы каноническим `VoiceStore.read`
   из окончательной новой `data` под целевым Windows owner и сравнить envelope hashes.
   Сохранить отдельный receipt этой проверки; только затем запускать приложение.

Эти шаги подготовки окончательной копии не выполняются командой `import` автоматически.
При несовпадении hash/ссылок/owner остановиться и сохранить isolated candidate для
диагностики. Исходный backup остаётся точкой восстановления.

Реализация ограничена 32 envelopes и 20 MiB исходного ciphertext. На диске возникают
только AES ciphertext, отдельный key file, DPAPI ciphertext и metadata receipts.
Python/.NET работают с plaintext в оперативной памяти; это не гарантия отсутствия
страниц памяти в системном pagefile или дампах ОС. Проверка на одном Windows owner
не заменяет проверку импорта под owner целевого компьютера. Исходный backup и ключ
нужно сохранить до этой проверки.
