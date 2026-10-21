"use client";

import { Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export type Offer = { title: string; code: string; max_discount: string; valid_until: string; details: string };

const EMPTY: Offer = { title: "", code: "", max_discount: "", valid_until: "", details: "" };

/** The campaign's offers as kept in its details: a JSON list (call_types.offers_of reads the same). */
export function parseOffers(raw: string | undefined): Offer[] {
  try {
    const list = JSON.parse(raw || "[]");
    return Array.isArray(list) ? list.map((o) => ({ ...EMPTY, ...(o && typeof o === "object" ? o : {}) })) : [];
  } catch {
    return [];
  }
}

const filled = (o: Offer) => !!(o.title.trim() || o.details.trim());

/** Store only the offers with something in them. */
export const offersJson = (list: Offer[]) => JSON.stringify(list.filter(filled).map((o) => ({ ...o, code: o.code.trim().toUpperCase() })));

/** Any number of offers, discounts or coupons the agent may mention, each with its own words. */
export function OffersEditor({ value, onChange }: { value: string | undefined; onChange: (json: string) => void }) {
  const saved = parseOffers(value);
  const list = saved.length ? saved : [EMPTY];
  const put = (next: Offer[]) => onChange(JSON.stringify(next));
  const edit = (i: number, k: keyof Offer, v: string) => put(list.map((o, j) => (j === i ? { ...o, [k]: v } : o)));

  return (
    <div className="space-y-3">
      <div>
        <div className="text-[13.5px] font-medium">Offers, discounts and coupons</div>
        <p className="text-[12px] text-muted-foreground">
          The agent may mention these, once and only when it fits (price, discount or delivery questions). It says nothing that is not written here.
        </p>
      </div>
      {list.map((o, i) => (
        <div key={i} className="rounded-lg border bg-muted/20 p-3.5">
          <div className="mb-3 flex items-center gap-2">
            <span className="text-[12.5px] font-medium text-muted-foreground">Offer {i + 1}</span>
            <button
              type="button"
              onClick={() => put(list.filter((_, j) => j !== i))}
              className="ml-auto rounded p-1 text-muted-foreground hover:bg-bad-soft hover:text-bad"
              aria-label={`Remove offer ${i + 1}`}
              title="Remove this offer"
            >
              <Trash2 className="size-4" />
            </button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Offer">
              <input className={inputClass} placeholder="Free shipping + 10% off" value={o.title} onChange={(e) => edit(i, "title", e.target.value)} />
            </Field>
            <Field label="Offer code" optional>
              <input
                className={cn(inputClass, "font-mono uppercase")}
                placeholder="COMEBACK10"
                value={o.code}
                onChange={(e) => edit(i, "code", e.target.value)}
              />
            </Field>
            <Field label="Max discount" optional>
              <input className={inputClass} placeholder="15%" value={o.max_discount} onChange={(e) => edit(i, "max_discount", e.target.value)} />
            </Field>
            <Field label="Offer valid until" optional hint="After this day the agent no longer mentions it.">
              <input className={inputClass} type="date" value={o.valid_until} onChange={(e) => edit(i, "valid_until", e.target.value)} />
            </Field>
            <div className="sm:col-span-2">
              <Field label="About the offer" optional hint="In your own words: who gets it, on what, any condition.">
                <textarea
                  className={cn(inputClass, "h-auto min-h-18 resize-y py-2 leading-snug")}
                  placeholder="Prepaid orders par free home delivery. Code COMEBACK10 se next order par 10% off, ₹999 se upar."
                  value={o.details}
                  onChange={(e) => edit(i, "details", e.target.value)}
                />
              </Field>
            </div>
          </div>
        </div>
      ))}
      <Button variant="outline" size="sm" onClick={() => put([...list, EMPTY])}>
        <Plus />
        Add offer
      </Button>
    </div>
  );
}
