"use client";

import { Grid3x3 } from "lucide-react";
import { cn } from "@/lib/utils";

/** What the keys do, by the campaign's call type. */
const WHAT: Record<string, string> = {
  abandoned_cart:
    "The call opens with the cart and the keys: 1 to order, 2 if not interested, 3 for a question, 0 to hear it again. After 1 it asks: 1 for prepaid (the link and coupon go on WhatsApp), 2 for cash on delivery (the address is checked or taken, and the order is booked on the Draft orders page).",
  campaign_invite:
    "The call opens with the campaign's opening and the keys: 1 for yes (interested: the campaign's WhatsApp message goes, if it has one), 2 if not interested, 3 for a question, 0 to hear the question again.",
  order_confirmation:
    "The call opens with the order and the keys: 1 to confirm, 2 to cancel, 3 for a question, 0 to hear it again. 2 asks once more before cancelling (1 cancels, 2 goes back). 1 ends the call (it asks for the address first only if the order has none).",
};

/**
 * "Keypad options" (owner, 2 Nov 2026; invites 3 Nov): the call opens with them; the customer can press 1 (confirm the
 * order / wants the cart / interested), 2 (cancel / not interested), 3 to ask a question, or 0 to hear it again.
 * value: "on" or "" (off), kept in the campaign's details as keypad. callType: the campaign's, for the wording.
 */
export function KeypadOptions({ value, onChange, callType }: { value: string | undefined; onChange: (value: string) => void; callType: string }) {
  const on = (value ?? "").trim().toLowerCase() === "on";
  const what = WHAT[callType] ?? WHAT.order_confirmation;

  return (
    <div className="rounded-lg border p-4">
      <label className="flex cursor-pointer items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-muted text-foreground">
          <Grid3x3 className="size-4.5" />
        </span>
        <span className="flex-1">
          <span className="block text-[13.5px] font-medium">Keypad options</span>
          <span className="block text-[12px] leading-snug text-muted-foreground">
            {what} Customers can still answer by voice. Phone calls on the Sarvam agent only.
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          onClick={() => onChange(on ? "" : "on")}
          className={cn("relative mt-1 h-6 w-11 shrink-0 rounded-full transition-colors", on ? "bg-primary" : "bg-input")}
        >
          <span className={cn("absolute top-0.5 size-5 rounded-full bg-card shadow transition-[left]", on ? "left-5.5" : "left-0.5")} />
        </button>
      </label>
    </div>
  );
}
