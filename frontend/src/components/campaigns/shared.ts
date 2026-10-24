// Small things the campaign pages share.
import type { Tone } from "@/components/ui/badge";
import type { CampaignStatus } from "@/lib/api";

/** A campaign's status tag. Finished = every customer on the list has had a first call, even if some call-backs
 *  are still waiting (owner, 24 Oct 2026: a list rung end to end showed "Paused"). */
export const CAMPAIGN_STATUS: Record<CampaignStatus, { label: string; tone: Tone; hint: string }> = {
  running: { label: "Running", tone: "info", hint: "Calling now" },
  scheduled: { label: "Scheduled", tone: "warn", hint: "Starts by itself at a set time" },
  paused: { label: "Paused", tone: "warn", hint: "Started, but some customers have not had their first call yet" },
  completed: { label: "Finished", tone: "good", hint: "Every customer on the list has had a first call" },
  draft: { label: "Draft", tone: "neutral", hint: "Not started yet" },
};

/** The tag beside the status: what is still left to ring. Never-called customers first, as the dialer takes them. */
export const leftTag = (newLeft: number, callbacks: number): { label: string; tone: Tone; hint: string } | null =>
  newLeft > 0
    ? { label: `${newLeft.toLocaleString("en-IN")} not called yet`, tone: "warn", hint: "Never rung: they get their first call on Start" }
    : callbacks > 0
      ? { label: `${callbacks.toLocaleString("en-IN")} call-backs left`, tone: "info", hint: "Rung before (no answer, busy or cut off): rung again on Start" }
      : null;

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
