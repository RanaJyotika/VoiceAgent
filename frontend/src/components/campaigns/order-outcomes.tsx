"use client";

import { Card } from "@/components/ui/card";
import { fmtInt, fmtPct, pct } from "@/lib/format";

/** /api/sales/confirmations, and a confirmation campaign's stats: the store's outcome of each order, grouped by what
 *  happened on its confirmation call (sales.py). */
export type OrderOutcomes = {
  groups: {
    key: string;
    label: string;
    orders: number;
    known: number; // orders the store's database had an outcome for
    delivered: number;
    cancelled: number;
    returned: number; // NDR & RTO
    open: number; // new, on hold, in transit...
  }[];
  last_check: string;
  error: string;
};

/** Owner, 26 Oct 2026: whether confirmation calls bring more orders to the door. Rates are over the finished orders
 *  only (delivered, cancelled or returned): most recent orders are still on their way. */
export function OrderOutcomesCard({ data }: { data: OrderOutcomes | null | undefined }) {
  const rows = (data?.groups ?? []).filter((g) => g.orders > 0);
  return (
    <Card className="overflow-hidden">
      <div className="border-b px-4 py-3">
        <h2 className="text-[15px] font-medium">What became of the orders</h2>
        <p className="mt-0.5 text-[12.5px] text-muted-foreground">
          From the store&apos;s database. Delivered, cancelled and returned are shares of the orders that are finished; the rest are still on their way.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-left text-[13.5px]">
          <thead className="text-[12.5px] text-muted-foreground">
            <tr className="border-b">
              <th className="px-4 py-2 font-normal">On the call</th>
              <th className="px-4 py-2 text-right font-normal">Orders</th>
              <th className="px-4 py-2 text-right font-normal">Delivered</th>
              <th className="px-4 py-2 text-right font-normal">Cancelled</th>
              <th className="px-4 py-2 text-right font-normal">Returned (RTO)</th>
              <th className="px-4 py-2 text-right font-normal">Still open</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((g) => {
              const done = g.delivered + g.cancelled + g.returned;
              const cell = (n: number) => (
                <td className="px-4 py-2.5 text-right tabular">
                  {fmtInt(n)}
                  {done > 0 && <span className="ml-1.5 text-muted-foreground">{fmtPct(pct(n, done), 0)}</span>}
                </td>
              );
              return (
                <tr key={g.key} className="border-b last:border-0">
                  <td className="px-4 py-2.5">{g.label}</td>
                  <td className="px-4 py-2.5 text-right tabular">{fmtInt(g.orders)}</td>
                  {cell(g.delivered)}
                  {cell(g.cancelled)}
                  {cell(g.returned)}
                  <td className="px-4 py-2.5 text-right text-muted-foreground tabular">{fmtInt(g.open + g.orders - g.known)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {data && rows.length === 0 && <p className="px-4 py-3 text-muted-foreground">No orders on this list yet.</p>}
        {!data && <p className="px-4 py-3 text-muted-foreground">Loading…</p>}
        {data?.error && <p className="px-4 py-3 text-[12.5px] text-bad">The store&apos;s database could not be read: {data.error}</p>}
      </div>
    </Card>
  );
}
