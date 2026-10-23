"use client";

import { RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { usePoll } from "@/hooks/use-poll";
import { send, type BatchReport } from "@/lib/api";
import { fmtRs } from "@/lib/format";
import { cn } from "@/lib/utils";
import { when } from "./shared";

const rs = (n: number | null) => (n == null ? "—" : fmtRs(n, 2));
const batchDay = (day: string) => new Date(`${day}T00:00:00`).toLocaleDateString("en-IN", { day: "2-digit", month: "short" });

/** Each batch of automatic calling: what it reached and what it cost (owner, 19 Oct 2026). */
export function BatchesCard() {
  const { toast, confirm } = useFeedback();
  const { data: d, reload } = usePoll<BatchReport>("/api/store/batches", 30_000);

  const startNew = async () => {
    const ok = await confirm({
      title: "Start a new batch now?",
      body: 'The next orders automatic calling rings count as a new batch, and the "Calls a day" number counts afresh from now (for example, the next 100 today).',
      confirmLabel: "Start a new batch",
    });
    if (!ok) return;
    try {
      await send("/api/store/batches/new", "POST");
      toast("New batch started.");
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  // A day can hold several batches: number them within the day ("19 Oct · 2").
  const perDay: Record<string, number> = {};
  const n = new Map<number, number>();
  for (const b of [...(d?.batches ?? [])].reverse()) n.set(b.id, (perDay[b.day] = (perDay[b.day] || 0) + 1));
  const c = d?.current;

  const heads = ["Batch", "Called", "Answered", "Confirmed", "Cancelled", "Didn't pick", "Hung up", "Other", "Talk time", "Cost", "Per order", "Per confirmed"];
  return (
    <Card className="overflow-hidden">
      <CardHeader className="flex-wrap">
        <div className="min-w-0">
          <CardTitle>Batches</CardTitle>
          <p className="text-[12.5px] text-muted-foreground">
            {c?.id
              ? `Now: batch ${c.id}, ${c.picked} called${c.limit ? ` of ${c.limit} (${c.room} left)` : " (no daily limit)"}, since ${when(c.started_at)}`
              : "The next batch starts with the next order automatic calling rings."}
          </p>
        </div>
        <Button size="sm" className="ml-auto" onClick={startNew}>
          <RotateCcw />
          Start a new batch
        </Button>
      </CardHeader>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1000px] text-left text-[13px]">
          <thead className="text-[12.5px] text-muted-foreground">
            <tr className="border-b">
              {heads.map((h, i) => (
                <th key={h} className={cn("px-4 py-2.5 font-normal", i > 0 && "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {d?.batches.map((b) => (
              <tr key={b.id} className="hover:bg-muted/40">
                <td className="px-4 py-2.5">
                  <div className="font-medium">
                    {batchDay(b.day)}
                    {(n.get(b.id) ?? 1) > 1 ? ` · ${n.get(b.id)}` : ""}
                  </div>
                  <div className="text-[12px] text-muted-foreground">
                    {when(b.started_at)}
                    {b.size ? ` · limit ${b.size}` : ""}
                  </div>
                </td>
                <td className="px-4 py-2.5 text-right tabular">
                  {b.orders}
                  <div className="text-[12px] text-muted-foreground">
                    {b.calls} call{b.calls === 1 ? "" : "s"}
                  </div>
                </td>
                <td className="px-4 py-2.5 text-right tabular">{b.answered}</td>
                <td className="px-4 py-2.5 text-right text-good tabular">{b.confirmed}</td>
                <td className="px-4 py-2.5 text-right text-bad tabular">{b.cancelled}</td>
                <td className="px-4 py-2.5 text-right tabular">{b.not_reached}</td>
                <td className="px-4 py-2.5 text-right tabular" title="Picked up, then the call ended before an answer">
                  {b.hung_up}
                </td>
                <td className="px-4 py-2.5 text-right tabular" title="Call back later, still waiting, or needs a look">
                  {b.call_later + b.waiting + b.other}
                </td>
                <td className="px-4 py-2.5 text-right tabular">{b.minutes} min</td>
                <td className="px-4 py-2.5 text-right font-medium tabular">{rs(b.cost_rs)}</td>
                <td className="px-4 py-2.5 text-right tabular">{rs(b.per_order_rs)}</td>
                <td className="px-4 py-2.5 text-right tabular">{rs(b.per_confirmed_rs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {d && d.batches.length === 0 && (
          <p className="px-4 py-8 text-center text-muted-foreground">No batches yet. Each day&apos;s automatic calling of store orders makes one.</p>
        )}
      </div>
    </Card>
  );
}
