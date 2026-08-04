export function initializeTelegram(): void {
  const webApp = window.Telegram?.WebApp;
  if (!webApp) return;
  webApp.ready();
  webApp.expand();
  document.documentElement.dataset.telegramTheme = webApp.colorScheme;
}

export function authorizationHeader(): string {
  const initData = window.Telegram?.WebApp?.initData;
  return initData ? `tma ${initData}` : "dev 1";
}

export function isInsideTelegram(): boolean {
  return Boolean(window.Telegram?.WebApp?.initData);
}

