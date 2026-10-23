import type { Tone } from "@/components/ui/badge";
import type { OrderItem, OrderQuota, OrderTag } from "@/lib/api";
import { cn } from "@/lib/utils";

/** The server's colour names (orders_view.STATES and the tags) as badge tones. */
export const toneOf = (t: string): Tone =>
  (({ warning: "warn", accent: "info", good: "good", critical: "bad", serious: "warn" })[t] as Tone | undefined) ?? "neutral";

export const when = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

export const rupees = (a: string | number | null | undefined) => {
  const n = Number(String(a ?? "").replace(/,/g, ""));
  return a && Number.isFinite(n) ? `₹${n.toLocaleString("en-IN")}` : "";
};

/** "12 s ago", "4 min ago", else the time; asOf: when the data arrived (no clock read while drawing). */
export function ago(iso: string | null | undefined, asOf: number) {
  if (!iso) return "not yet";
  const s = Math.max(0, Math.round((asOf - new Date(iso).getTime()) / 1000));
  return s < 60 ? `${s} s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : when(iso);
}

export const PICK_TEXT: Record<string, string> = { newest: "newest first", oldest: "oldest first", random: "picked at random" };

/** Which orders automatic calling rings, in a sentence: "at most 100 a day, from today's orders, newest first". */
export function quotaText(q: OrderQuota | undefined) {
  if (!q) return "";
  const days = (q.days_label || "Any waiting order").toLowerCase();
  const pick = PICK_TEXT[q.pick] ?? q.pick;
  return q.limit ? `at most ${q.limit} a day, from ${days}, ${pick} (${q.picked_today} in this batch)` : `${days}, ${pick}, no daily limit`;
}

/** On a row only the tags that matter most, in this order; the side panel shows them all. */
const TAG_ORDER = [
  "Return risk",
  "High Risk",
  "Bad Address",
  "COD cancelled (store)",
  "Has a question",
  "VIP customer",
  "COD confirmed (store)",
  "High value",
  "Repeat customer",
  "New customer",
];
export const topTags = (tags: OrderTag[], n = 2) =>
  tags
    .filter((t) => TAG_ORDER.includes(t.label))
    .sort((a, b) => TAG_ORDER.indexOf(a.label) - TAG_ORDER.indexOf(b.label))
    .slice(0, n);

const toneText: Record<Tone, string> = {
  good: "border-good/25 bg-good-soft text-good",
  bad: "border-bad/25 bg-bad-soft text-bad",
  warn: "border-warn/25 bg-warn-soft text-warn",
  info: "border-info/25 bg-info-soft text-info",
  neutral: "border-border bg-muted text-muted-foreground",
};

export function TagChip({ tag }: { tag: OrderTag }) {
  return (
    <span className={cn("inline-flex items-center rounded-full border px-2 py-0.5 text-[11.5px] font-medium whitespace-nowrap", toneText[toneOf(tag.tone)])}>
      {tag.label}
    </span>
  );
}

/** The products of an order: a small photo, the name, variant and quantity. */
export function Items({ items, size = 36 }: { items: OrderItem[]; size?: number }) {
  return (
    <div className="space-y-2">
      {items.map((i, k) => (
        <div key={k} className="flex items-center gap-2.5">
          {i.image ? (
            // eslint-disable-next-line @next/next/no-img-element -- store photos by their own address, in a static export
            <img
              src={`${i.image}${i.image.includes("?") ? "&" : "?"}width=${size * 2}`}
              alt=""
              loading="lazy"
              className="shrink-0 rounded-md border bg-muted object-cover"
              style={{ width: size, height: size }}
            />
          ) : (
            <span className="shrink-0 rounded-md border bg-muted" style={{ width: size, height: size }} />
          )}
          <span className="min-w-0">
            <span className="line-clamp-2 text-[13px] leading-snug font-medium">{i.title}</span>
            <span className="block text-[12px] text-muted-foreground">
              {i.variant ? `${i.variant} · ` : ""}Qty {i.qty}
            </span>
          </span>
        </div>
      ))}
    </div>
  );
}
