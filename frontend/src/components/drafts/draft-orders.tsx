"use client";

import { useState } from "react";
import { ExternalLink, MessageSquare } from "lucide-react";
import { CallPanel } from "@/components/overview/call-panel";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { Pager, usePaged } from "@/components/ui/pager";
import { usePoll } from "@/hooks/use-poll";
import { send, type DraftOrder, type DraftOrders as Data } from "@/lib/api";
import { fmtPhone, fmtRs } from "@/lib/format";
import { cn } from "@/lib/utils";

type Show = "pending" | "created" | "all";

/** Where the address came from (draft_orders.address_source). */
const ADDRESS: Record<string, { label: string; tone: Tone }> = {
  on_record: { label: "Confirmed on the call", tone: "good" },
  taken_on_call: { label: "Taken on the call", tone: "info" },
  missing: { label: "Address missing", tone: "bad" },
};

const fmtWhen = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

const amountOf = (a: string) => {
  const n = Number(String(a || "").replace(/[^\d.]/g, ""));
  return a && Number.isFinite(n) && n > 0 ? fmtRs(n) : a || "—";
};

/** Owner, 3 Nov 2026: abandoned-cart customers who chose cash on delivery on a keypad call and confirmed their address.
 *  The team creates each order in the store by hand, then ticks it here. */
export function DraftOrders() {
  const [show, setShow] = useState<Show>("pending");
  const { data, error, reload } = usePoll<Data>(`/api/draft-orders?show=${show}`, 15_000);
  const { toast } = useFeedback();
  const [refs, setRefs] = useState<Record<number, string>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [openCall, setOpenCall] = useState<number | null>(null);
  const rows = data?.rows ?? [];
  const paged = usePaged(rows, 25, show);

  async function mark(d: DraftOrder, created: boolean) {
    setBusy(d.id);
    try {
      await send(`/api/draft-orders/${d.id}`, "POST", { order_created: created, order_ref: refs[d.id] ?? d.order_ref ?? "" });
      toast(created ? "Marked as created in the store." : "Marked as not created yet.");
      reload();
    } catch (e) {
      toast(`Could not save: ${(e as Error).message}`, true);
    } finally {
      setBusy(null);
    }
  }

  const chip = (key: Show, label: string, n?: number) => (
    <button
      key={key}
      type="button"
      aria-pressed={show === key}
      onClick={() => setShow(key)}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[12.5px]",
        show === key ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
      )}
    >
      {label}
      {n != null && <b className="tabular">{n}</b>}
    </button>
  );

  return (
    <div className="pb-10">
      <div className="border-b px-4 pt-5 pb-4 sm:px-6">
        <h1 className="text-[20px] font-semibold tracking-tight">Draft orders</h1>
        <p className="mt-0.5 text-[13.5px] text-muted-foreground">
          Cart customers who chose cash on delivery on the call. Create each order in the store, then tick it here.
        </p>
      </div>

      <div className="space-y-4 px-4 pt-4 sm:px-6">
        <div className="flex flex-wrap items-center gap-1.5">
          {chip("pending", "To create", data?.pending)}
          {chip("created", "Created", data ? data.total - data.pending : undefined)}
          {chip("all", "All", data?.total)}
        </div>

        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1150px] text-left text-[13.5px]">
              <thead className="text-[12.5px] text-muted-foreground">
                <tr className="border-b">
                  {["Customer", "Product", "Amount", "Delivery address", "Booked", "Order created", ""].map((h, i) => (
                    <th key={i} className={cn("px-4 py-2.5 font-normal", h === "Amount" && "text-right")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {paged.rows.map((d) => {
                  const src = ADDRESS[d.address_source] ?? ADDRESS.missing;
                  const done = d.order_created === 1;
                  return (
                    <tr key={d.id} className={cn("align-top hover:bg-muted/40", done && "opacity-70")}>
                      <td className="px-4 py-3">
                        <div className="font-medium">{d.customer_name || <span className="font-normal text-muted-foreground">No name</span>}</div>
                        <div className="font-mono text-[12px] text-muted-foreground">{d.phone ? fmtPhone(d.phone) : "no phone"}</div>
                      </td>
                      <td className="max-w-72 min-w-52 px-4 py-3">
                        <div>{d.product || "—"}</div>
                        <div className="mt-0.5 text-[12px] text-muted-foreground">
                          Qty {d.quantity || 1}
                          {d.product_ids ? ` · ID ${d.product_ids}` : ""}
                        </div>
                        {d.cart_url && (
                          <a
                            href={d.cart_url}
                            target="_blank"
                            rel="noreferrer"
                            className="mt-1 inline-flex items-center gap-1 text-[12px] text-primary hover:underline"
                          >
                            Cart link <ExternalLink className="size-3" />
                          </a>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right whitespace-nowrap tabular">
                        {amountOf(d.amount)}
                        <div className="mt-1">
                          <Badge tone="warn">{d.payment || "COD"}</Badge>
                        </div>
                      </td>
                      <td className="max-w-80 min-w-60 px-4 py-3">
                        <div className="leading-snug">{d.address || <span className="text-muted-foreground">Not given</span>}</div>
                        <div className="mt-1">
                          <Badge tone={src.tone}>{src.label}</Badge>
                        </div>
                      </td>
                      <td className="px-4 py-3 text-[12.5px] whitespace-nowrap text-muted-foreground">
                        {fmtWhen(d.created_at)}
                        <div>{d.campaign_name || ""}</div>
                      </td>
                      <td className="min-w-56 px-4 py-3">
                        <label className="flex cursor-pointer items-center gap-2">
                          <input
                            type="checkbox"
                            className="size-4 accent-[var(--color-primary)]"
                            checked={done}
                            disabled={busy === d.id}
                            onChange={(e) => mark(d, e.target.checked)}
                          />
                          <span className={cn("text-[13px]", done ? "font-medium text-good" : "text-muted-foreground")}>
                            {done ? "Created" : "Not created yet"}
                          </span>
                        </label>
                        <input
                          aria-label="Store order number"
                          placeholder="Store order no. (optional)"
                          className="mt-1.5 h-8 w-44 rounded-md border border-input bg-card px-2 text-[12.5px]"
                          value={refs[d.id] ?? d.order_ref ?? ""}
                          onChange={(e) => setRefs({ ...refs, [d.id]: e.target.value })}
                          onBlur={() => done && refs[d.id] !== undefined && refs[d.id] !== (d.order_ref ?? "") && mark(d, true)}
                        />
                        {done && d.order_created_at && <div className="mt-1 text-[11.5px] text-muted-foreground">{fmtWhen(d.order_created_at)}</div>}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex justify-end">
                          {d.call_id != null && (
                            <Button size="sm" variant="ghost" onClick={() => setOpenCall(d.call_id)}>
                              <MessageSquare />
                              View call
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {data && rows.length === 0 && (
              <p className="px-4 py-10 text-center text-muted-foreground">
                {show === "pending" ? "No draft orders waiting to be created." : show === "created" ? "None created yet." : "No draft orders yet."}
              </p>
            )}
            {!data && <p className="px-4 py-10 text-center text-muted-foreground">{error ? `Could not load the list: ${error}` : "Loading…"}</p>}
          </div>
          <Pager page={paged.page} pages={paged.pages} onPage={paged.setPage} total={paged.total} size={paged.size} />
        </Card>
        <p className="text-[12.5px] text-muted-foreground">
          Booked on abandoned-cart calls with Keypad options on, when the customer chose cash on delivery and confirmed (or gave) the address. No WhatsApp link
          is sent for these.
        </p>
      </div>

      {openCall != null && <CallPanel key={openCall} callId={openCall} onClose={() => setOpenCall((c) => (c === openCall ? null : c))} />}
    </div>
  );
}
