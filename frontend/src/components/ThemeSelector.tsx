import { AppTheme } from "../theme";

export function ThemeSelector({ theme, onThemeChange, compact = false }: { theme: AppTheme; onThemeChange: (theme: AppTheme) => void; compact?: boolean }) {
  return (
    <button className={`theme-selector ${compact ? "compact" : ""}`} type="button" onClick={() => onThemeChange(theme === "default" ? "cockpit" : "default")} aria-label={`Switch to ${theme === "default" ? "cockpit" : "default"} theme`} title="Switch visual theme">
      <span className="theme-selector-orb" aria-hidden="true" />
      {!compact && <span>{theme === "cockpit" ? "Cockpit" : "Classic"}</span>}
    </button>
  );
}
