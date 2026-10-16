export const timeOffset = (milliseconds: number | null | undefined): string => {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return 'Время неизвестно';
  const total = Math.floor(milliseconds / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return `${hours > 0 ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
};

const binaryUnits = ['байт', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ', 'ПиБ'];

export const formatBinaryBytes = (bytes: number): string => {
  if (!Number.isSafeInteger(bytes) || bytes < 0) return 'Неизвестно';
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < binaryUnits.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} ${binaryUnits[unit]}`;
};

export const importLimitHint = (maxUploadBytes: number | null | undefined): string => {
  const limit = typeof maxUploadBytes === 'number' && Number.isSafeInteger(maxUploadBytes) && maxUploadBytes >= 1024
    ? `максимум ${formatBinaryBytes(maxUploadBytes)}`
    : 'лимит размера не получен';
  return `Аудио или видео · до 4 часов · ${limit}`;
};
