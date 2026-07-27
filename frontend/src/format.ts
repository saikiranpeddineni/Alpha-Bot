// Number/time formatting helpers (tabular, terminal-style).

const usd = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const usd0 = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const MARKET_TIME_ZONE = "America/New_York";

export const fmtMoney = (v: number | null | undefined): string =>
  v == null ? "—" : usd.format(v);

export const fmtMoney0 = (v: number | null | undefined): string =>
  v == null ? "—" : usd0.format(v);

export const fmtSigned = (v: number | null | undefined): string => {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${usd.format(v).replace("$-", "-$")}`;
};

export const fmtPct = (v: number | null | undefined, digits = 2): string =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(digits)}%`;

export const fmtNum = (v: number | null | undefined): string => (v == null ? "—" : num.format(v));

export const fmtQty = (v: number): string => (Number.isInteger(v) ? String(v) : v.toFixed(2));

export const fmtCompact = (v: number | null | undefined): string => {
  if (v == null) return "—";
  if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return num.format(v);
};

export const fmtTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    timeZone: MARKET_TIME_ZONE,
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

export const fmtDate = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    timeZone: MARKET_TIME_ZONE,
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
};

export const fmtDateTime = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-US", {
    timeZone: MARKET_TIME_ZONE,
    month: "short",
    day: "numeric",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
};

export const pnlClass = (v: number | null | undefined): string =>
  v == null ? "" : v >= 0 ? "pos" : "neg";
