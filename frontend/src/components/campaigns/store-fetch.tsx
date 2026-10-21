"use client";

import { useState } from "react";
import { Check, CheckCircle2, ShoppingBag } from "lucide-react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { Select } from "@/components/ui/select";
import { useStoreTags } from "@/hooks/use-static";
import { send } from "@/lib/api";
import { fmtPhone } from "@/lib/format";
import { cn } from "@/lib/utils";
import { plural } from "./shared";

type Fetched = {
  order_id: string;
  customer_name: string;
  phone: string;
  product: string;
  amount: string;
  payment_kind: string;
  tags: string[];
  result: "added" | "on_list" | "elsewhere" | "no_phone" | "do_not_call";
};

type Result = {
  imported: number;
  skipped: number;
  total: number;
  left_out: { no_phone: number; on_list: number; elsewhere: number; do_not_call: number };
  rows: Fetched[]; // every order the filters matched, added or not
  /** The server answered without the list: it runs older code and needs a restart. */
  stale?: boolean;
};

/** What happened to each fetched order, as the table says it. */
const RESULT: Record<Fetched["result"], { label: string; tone: Tone }> = {
  added: { label: "Added", tone: "good" },
  on_list: { label: "On this list", tone: "good" },
  elsewhere: { label: "On another call list", tone: "warn" },
  no_phone: { label: "No valid phone", tone: "bad" },
  do_not_call: { label: "Asked not to be called", tone: "bad" },
};

const today = () => new Date(Date.now() + 330 * 60_000).toISOString().slice(0, 10); // Indian date

/** Audience from the store: the orders of the chosen days, payment and tags (main database, read only) are added to
 *  this campaign's list at once. That list, shown under this, is who the campaign calls. */
export function StoreFetch({ campaignId, onChanged }: { campaignId: number; onChanged: () => void }) {
  const { toast } = useFeedback();
  const allTags = useStoreTags();
  const [from, setFrom] = useState(today);
  const [to, setTo] = useState(today);
  const [payment, setPayment] = useState("cod");
  const [includeListed, setIncludeListed] = useState(false);
  const [tags, setTags] = useState<string[]>([]); // keys of /api/store/order-tags
  const [tagMode, setTagMode] = useState<"only" | "except">("only");
  const [result, setResult] = useState<Result | null>(null);
  const [show, setShow] = useState<"all" | Fetched["result"]>("all");
  const [busy, setBusy] = useState(false);

  const label = (k: string) => allTags?.find((x) => x.key === k)?.label ?? k;
  const changed = (f: () => void) => () => {
    f();
    setResult(null); // the result was for the filters as they were
  };

  /** Fetch with the filters as set. withListed: also add the ones on another call list ("Add these too"). */
  const fetchOrders = async (withListed = includeListed) => {
    setBusy(true);
    try {
      const r = await send<Partial<Result>>(`/api/campaigns/${campaignId}/from-store`, "POST", {
        day_from: from,
        day_to: to,
        payment,
        include_listed: withListed,
        tags,
        tag_mode: tagMode,
      });
      if (withListed && !includeListed && result) {
        // The second pass only says which were added now; the first ones are on this list from before.
        toast(`Added ${plural(r.imported ?? 0, "more customer")}.`);
      }
      // A server started before this page was built answers without the list (or the counts): never crash on it.
      setResult({
        imported: r.imported ?? 0,
        skipped: r.skipped ?? 0,
        total: r.total ?? (r.imported ?? 0) + (r.skipped ?? 0),
        left_out: { no_phone: 0, on_list: 0, elsewhere: 0, do_not_call: 0, ...(r.left_out ?? {}) },
        rows: Array.isArray(r.rows) ? r.rows : [],
        stale: !Array.isArray(r.rows),
      });
      setShow("all");
      onChanged();
    } catch (e) {
      toast(`Could not fetch the orders: ${(e as Error).message}`, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Orders from">
          <input className={inputClass} type="date" value={from} max={to} onChange={(e) => changed(() => setFrom(e.target.value))()} />
        </Field>
        <Field label="To">
          <input className={inputClass} type="date" value={to} min={from} max={today()} onChange={(e) => changed(() => setTo(e.target.value))()} />
        </Field>
        <Field label="Payment">
          <Select tall value={payment} onChange={(e) => changed(() => setPayment(e.target.value))()}>
            <option value="cod">Cash on delivery</option>
            <option value="prepaid">Prepaid</option>
            <option value="all">All orders</option>
          </Select>
        </Field>
      </div>

      <div>
        <div className="mb-2 flex flex-wrap items-center gap-2 text-[13.5px]">
          <div className="flex rounded-md bg-muted p-0.5" role="group" aria-label="How the tags filter">
            {(
              [
                ["only", "Only orders that are"],
                ["except", "Leave out orders that are"],
              ] as const
            ).map(([k, text]) => (
              <button
                key={k}
                type="button"
                aria-pressed={tagMode === k}
                onClick={changed(() => setTagMode(k))}
                className={cn(
                  "h-7 rounded px-2.5 text-[13px]",
                  tagMode === k ? "bg-card font-medium shadow-sm" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {text}
              </button>
            ))}
          </div>
          <span className="text-[12.5px] text-muted-foreground">(tap to pick; any number at once)</span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {(allTags ?? []).map((tg) => {
            const on = tags.includes(tg.key);
            return (
              <button
                key={tg.key}
                type="button"
                aria-pressed={on}
                onClick={changed(() => setTags(on ? tags.filter((x) => x !== tg.key) : [...tags, tg.key]))}
                className={cn(
                  "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-[13px] transition-colors",
                  on ? "border-primary bg-primary text-primary-foreground" : "bg-muted/60 hover:bg-muted",
                )}
              >
                {on && <Check className="size-3.5" />}
                {tg.label}
              </button>
            );
          })}
        </div>
        <p className="mt-1.5 text-[12.5px] text-muted-foreground">
          {tags.length === 0
            ? "No tag picked: every order of those days."
            : tagMode === "only"
              ? `Only orders tagged ${tags.map(label).join(" or ")}.`
              : `Every order except those tagged ${tags.map(label).join(" or ")}.`}
        </p>
      </div>

      <label className="flex cursor-pointer items-center gap-2 text-[13px] text-muted-foreground">
        <input type="checkbox" className="accent-brand" checked={includeListed} onChange={(e) => changed(() => setIncludeListed(e.target.checked))()} />
        Include orders already on another call list (for example, the automatic Store orders calls). Off: nobody is called twice for one order.
      </label>

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="primary" onClick={() => fetchOrders()} disabled={busy || !from || !to}>
          <ShoppingBag />
          {busy ? "Fetching…" : "Fetch orders"}
        </Button>
        <span className="text-[12.5px] text-muted-foreground">Every matching order is listed; the ones added join the call list below.</span>
      </div>

      {result && (
        <div className="overflow-hidden rounded-lg border">
          <div className="flex flex-wrap items-start gap-3 border-b px-4 py-3">
            <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-good" />
            <div className="min-w-0 flex-1">
              <div className="text-[14px] font-medium">Fetched {plural(result.total, "customer")}</div>
              <div className="text-[12.5px] text-muted-foreground">
                {plural(result.imported, "customer")} added to this campaign now
                {result.skipped > 0 &&
                  ` · ${(Object.keys(RESULT) as Fetched["result"][])
                    .filter((k) => k !== "added" && result.left_out[k as keyof Result["left_out"]])
                    .map((k) => `${result.left_out[k as keyof Result["left_out"]]} ${RESULT[k].label.toLowerCase()}`)
                    .join(" · ")}`}
              </div>
            </div>
            {result.left_out.elsewhere > 0 && (
              <Button variant="outline" size="sm" onClick={() => fetchOrders(true)} disabled={busy} title="They may also be called by the other list">
                Add the {result.left_out.elsewhere} on another list too
              </Button>
            )}
          </div>
          {result.stale && (
            <p className="border-b bg-warn-soft px-4 py-2 text-[12.5px] text-warn">
              The server is running older code, so the list of fetched customers cannot be shown. Close the run_phone.bat window, start it again, then fetch
              once more.
            </p>
          )}
          <div className="flex flex-wrap gap-1.5 border-b px-4 py-2">
            {(["all", ...(Object.keys(RESULT) as Fetched["result"][])] as const)
              .filter((k) => k === "all" || result.rows.some((r) => r.result === k))
              .map((k) => (
                <button
                  key={k}
                  type="button"
                  aria-pressed={show === k}
                  onClick={() => setShow(k)}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[12.5px]",
                    show === k ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
                  )}
                >
                  {k === "all" ? "All" : RESULT[k].label}{" "}
                  <b className="ml-0.5 tabular">{k === "all" ? result.total : result.rows.filter((r) => r.result === k).length}</b>
                </button>
              ))}
          </div>
          <div className="max-h-105 overflow-auto">
            <table className="w-full min-w-[720px] text-left text-[13px]">
              <thead className="sticky top-0 bg-card text-[12px] text-muted-foreground">
                <tr className="border-b">
                  {["Order", "Customer", "Product", "Amount", "Payment", "Status"].map((h) => (
                    <th key={h} className="px-3 py-2 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {result.rows
                  .filter((r) => show === "all" || r.result === show)
                  .map((r) => (
                    <tr key={r.order_id} className="align-top">
                      <td className="px-3 py-2 font-mono text-[12.5px]">{r.order_id}</td>
                      <td className="px-3 py-2">
                        <div>{r.customer_name}</div>
                        <div className="font-mono text-[12px] text-muted-foreground">{fmtPhone(r.phone)}</div>
                      </td>
                      <td className="max-w-[240px] px-3 py-2">
                        {r.product}
                        {r.tags.length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {r.tags.map((x) => (
                              <Badge key={x}>{x}</Badge>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2 tabular">₹{r.amount}</td>
                      <td className="px-3 py-2">{r.payment_kind}</td>
                      <td className="px-3 py-2">
                        <Badge tone={RESULT[r.result].tone}>{RESULT[r.result].label}</Badge>
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {result.total > result.rows.length && (
            <p className="border-t px-4 py-2 text-[12.5px] text-muted-foreground">
              Showing {result.rows.length} of {result.total}; all of them are counted above.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
