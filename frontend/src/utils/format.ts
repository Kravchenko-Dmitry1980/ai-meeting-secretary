export const timeOffset = (milliseconds: number | null | undefined): string => {
  if (milliseconds == null || !Number.isFinite(milliseconds)) return 'Время неизвестно';
  const total = Math.floor(milliseconds / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return `${hours > 0 ? `${hours}:` : ''}${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
};
