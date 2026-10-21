// Small things the campaign pages share.

/** Results after which a contact needs no more calls: what "done" counts in a campaign's progress. */
export const FINAL = [
  "confirmed",
  "changed",
  "cancelled",
  "not_ordered",
  "wrong_number",
  "do_not_call",
  "unclear",
  "interested",
  "not_interested",
  "already_booked",
  "needs_help",
  "wants_to_order",
  "already_ordered",
  "wants_coupon",
];

/** Contacts waiting for another try. */
export const CALLBACK_STATUSES = ["no_answer", "busy", "dropped", "call_later"];

export const doneCount = (byStatus: Record<string, number>) => FINAL.reduce((n, k) => n + (byStatus[k] || 0), 0);

export const fmtDay = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }) : "");

export const fmtShort = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

export const campaignHref = (id: number) => `/campaigns/view/?id=${id}`;

export const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
