export type AppTheme = "default" | "cockpit";

const THEME_STORAGE_KEY = "alphadesk.theme";

export function readTheme(): AppTheme {
  if (typeof window === "undefined") return "default";
  return window.localStorage.getItem(THEME_STORAGE_KEY) === "cockpit" ? "cockpit" : "default";
}

export function applyTheme(theme: AppTheme): void {
  document.documentElement.dataset.theme = theme;
}

export function persistTheme(theme: AppTheme): void {
  window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  applyTheme(theme);
}
