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

/** An order number as stored: store orders carry their own "#" (#75944), sheet ones are as typed (VV-1001). */
export const fmtOrder = (id: string | null | undefined) => id ?? "";

/** The email a contact's sheet gave, from its extra columns ("Email ID", "email"...), or "". Owner, 24 Oct 2026: 147 of a
 *  cart campaign's 917 rows had no name at all (the export has none), so the table shows this, else "No name in the list". */
export function sheetEmail(extra: unknown): string {
  let cols: unknown = extra;
  if (typeof cols === "string") {
    try {
      cols = JSON.parse(cols);
    } catch {
      return "";
    }
  }
  if (!cols || typeof cols !== "object") return "";
  for (const [key, v] of Object.entries(cols as Record<string, unknown>)) {
    const label = typeof v === "object" && v ? String((v as { label?: string }).label ?? key) : key;
    const value = typeof v === "object" && v ? String((v as { value?: string }).value ?? "") : String(v ?? "");
    if (/e-?mail/i.test(`${key} ${label}`) && value.includes("@")) return value.trim();
  }
  return "";
}
