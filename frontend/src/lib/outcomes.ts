import type { Tone } from "@/components/ui/badge";

// What each call result is called on screen; the same names as the classic dashboard (backend/app/static/dashboard.js).
export const OUTCOMES: Record<string, { label: string; tone: Tone }> = {
  confirmed: { label: "Confirmed", tone: "good" },
  changed: { label: "Change requested", tone: "warn" },
  cancelled: { label: "Cancelled", tone: "bad" },
  not_ordered: { label: "Says not ordered", tone: "bad" },
  call_later: { label: "Call later", tone: "neutral" },
  wrong_number: { label: "Wrong number", tone: "bad" },
  do_not_call: { label: "Do not call", tone: "bad" },
  no_answer: { label: "No answer", tone: "neutral" },
  busy: { label: "Busy", tone: "neutral" },
  dropped: { label: "Call dropped", tone: "warn" },
  unclear: { label: "Needs review", tone: "warn" },
  interested: { label: "Interested", tone: "good" },
  not_interested: { label: "Not interested", tone: "neutral" },
  already_booked: { label: "Already booked", tone: "good" },
  needs_help: { label: "Wants help", tone: "warn" },
  wants_to_order: { label: "Wants to order", tone: "good" },
  already_ordered: { label: "Already ordered", tone: "good" },
  wants_coupon: { label: "Wants coupon", tone: "good" },
  complaint: { label: "Complaint", tone: "alert" },
  done: { label: "Done", tone: "good" },
  error: { label: "Error", tone: "bad" },
  none: { label: "No outcome", tone: "neutral" },
  // contact statuses that are not call results
  pending: { label: "To call", tone: "neutral" },
  calling: { label: "Calling now", tone: "info" },
};

/** What each result means, in a sentence: the hover text of the outcome tags in the campaign wizard. */
export const MEANINGS: Record<string, string> = {
  confirmed: "An AI call was placed and the customer confirmed the order.",
  changed: "The customer confirmed but asked for a change (address, product or date); the team checks it.",
  cancelled: "The customer asked to cancel; their reason is noted.",
  not_ordered: "The customer says they did not place this order.",
  call_later: "The customer asked to be called back later; a time is noted when they gave one.",
  wrong_number: "The number did not belong to the customer.",
  do_not_call: "The customer asked never to be called again; no campaign rings them again.",
  unclear: "The call needs a person to look at it: an unclear answer or something the customer disputed.",
  interested: "The customer is interested.",
  not_interested: "The customer is not interested; the reason is noted.",
  already_booked: "The customer has already booked.",
  needs_help: "The customer wants help the agent cannot give (a link, a person); the team follows up.",
  wants_to_order: "The customer wants to complete the order.",
  already_ordered: "The customer has already ordered.",
  wants_coupon: "The customer said yes to the coupon offer.",
  complaint: "The customer complained (fraud, refund, late order, payment, police); the team reviews it.",
  done: "The call was made and the customer answered.",
};

/** The tag the code puts on a Shopify order for a call's result (shopify._tag): "voice-confirmed". */
export const tagOf = (outcome: string) => `voice-${outcome.replace(/_/g, "-")}`;

export const outcomeOf = (key: string | null | undefined) => (key ? (OUTCOMES[key] ?? { label: key, tone: "neutral" as Tone }) : null);

/** What a complaint is about (outcome_details.complaint_type), as the team reads it. */
export const COMPLAINT_KINDS: Record<string, string> = {
  refund: "Refund",
  fraud: "Fraud / fake",
  late: "Order late",
  payment: "Payment issue",
  legal: "Police / legal",
  other: "Other",
};

/** What a complaint is about ("Refund", "Order late"), or "" when the call was no complaint. */
export const complaintKind = (details: Record<string, unknown> | null | undefined) => {
  const k = String(details?.complaint_type ?? "");
  return k ? (COMPLAINT_KINDS[k] ?? k) : "";
};
