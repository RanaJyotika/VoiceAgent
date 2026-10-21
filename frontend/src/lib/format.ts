const IN = "en-IN";

export const fmtInt = (n: number) => n.toLocaleString(IN);

export const fmtRs = (n: number, decimals = 0) => `₹${n.toLocaleString(IN, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}`;

export const pct = (part: number, whole: number) => (whole ? (100 * part) / whole : null);

export const fmtPct = (n: number | null, decimals = 1) => (n == null ? "—" : `${n.toFixed(decimals)}%`);

/** 125 -> "02:05" */
export const fmtClock = (seconds: number) => {
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
};

/** "+919820144120" or "9820144120" -> "+91 98201 44120" */
export const fmtPhone = (raw: string) => {
  const d = (raw || "").replace(/\D/g, "");
  const local = d.length > 10 ? d.slice(-10) : d;
  if (local.length !== 10) return raw;
  return `+91 ${local.slice(0, 5)} ${local.slice(5)}`;
};

export const fmtTime = (d: Date) => d.toLocaleTimeString(IN, { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export const fmtToday = () => new Date().toLocaleDateString("en-US", { weekday: "long", month: "short", day: "numeric" });
