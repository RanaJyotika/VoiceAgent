"use client";

import { usePoll } from "@/hooks/use-poll";
import { fmtInt, fmtPct, fmtRs, pct } from "@/lib/format";
import { ReachCards, type Reach } from "./reach-cards";
import { StatCard } from "./stat-card";

/** /api/overview/today: each customer phoned today, counted once, by the result of their latest call. */
type Today = {
  called: number;
  convinced: number;
  not_answered: number;
  busy: number;
  needs_review: number;
  hung_up: number;
  call_later: number;
  agent_rs: number;
  line_rs: number;
  calls: number;
  /** Store orders of the last 7 days (sales.py): after a talk with the agent (matched by phone), and from our WhatsApp
   * button (its UTMs). */
  week_sales?: Orders & { window_days: number; link?: Orders }; // missing from a server not restarted yet
  week_reach?: Reach; // customers called or messaged in the last 7 days
};

type Money = { orders: number; sales_rs: number };
/** COD counts part-paid COD too: the rest is paid at the door. */
type Orders = Money & { cod?: Money; prepaid?: Money };

/** The Overview's last row: today only, whatever period the filters above show. */
export function TodayCards() {
  const { data: d } = usePoll<Today>("/api/overview/today", 15_000);
  const v = (n: number | undefined) => (n == null ? "—" : fmtInt(n));
  const rest = d ? [d.hung_up && `${d.hung_up} picked up, no result`, d.call_later && `${d.call_later} asked to call later`].filter(Boolean).join(" · ") : "";

  return (
    <section aria-labelledby="today-title">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id="today-title" className="text-[15px] font-medium">
          Today
        </h2>
        <p className="text-[12.5px] text-muted-foreground">
          Each customer phoned today counts once, by the result of their latest call. Browser tests are not counted as customers.
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Customers called" value={v(d?.called)} note={d ? `${plural(d.calls, "call")} placed${rest ? ` · ${rest}` : ""}` : ""} />
        <StatCard
          label="Convinced"
          value={v(d?.convinced)}
          note={d?.called ? `${fmtPct(pct(d.convinced, d.called))} of customers called` : "Confirmed or said yes"}
        />
        <StatCard label="Not answered" value={v(d?.not_answered)} note="Did not pick up" />
        <StatCard label="Busy" value={v(d?.busy)} note="The line was busy" />
        <StatCard label="Needs review" value={v(d?.needs_review)} note="Unclear, wrong number, did not order, or an error" />
        <StatCard
          label="Agent cost today"
          value={d ? rs(d.agent_rs) : "—"}
          note={d ? `+ ${rs(d.line_rs)} phone line · ${rs(d.agent_rs + d.line_rs)} in all` : ""}
        />
      </div>
      {d?.week_reach && (
        <div className="mt-5">
          <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h3 className="text-[15px] font-medium">Customers reached · last 7 days</h3>
            <p className="text-[12.5px] text-muted-foreground">
              Each customer counted once: called, or sent a WhatsApp message, at least once. Ordered: by phone after a call, or
              from the WhatsApp button.
            </p>
          </div>
          <ReachCards r={d.week_reach} />
        </div>
      )}
      {d?.week_sales && <WeekSales w={d.week_sales} />}
    </section>
  );
}

/** Sales of the last 7 days: from the WhatsApp button (certain, by its UTMs), COD and prepaid apart, and after a talk. */
function WeekSales({ w }: { w: NonNullable<Today["week_sales"]> }) {
  const link = w.link ?? { orders: 0, sales_rs: 0 };
  const worth = (m?: Money) => (m?.orders ? fmtRs(m.sales_rs) : "None yet");
  const split = (o: Orders) => (o.cod && o.prepaid ? `COD ${fmtInt(o.cod.orders)} · Prepaid ${fmtInt(o.prepaid.orders)}` : "");
  return (
    <div className="mt-5">
      <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-[15px] font-medium">Sales from calls · last 7 days</h3>
        <p className="text-[12.5px] text-muted-foreground">
          Button orders are read from the order itself (the link&apos;s UTMs). Orders after a talk are matched by phone, within{" "}
          {w.window_days} days of the call.
        </p>
      </div>
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        <StatCard
          label="Orders from WhatsApp button"
          value={fmtInt(link.orders)}
          note={link.orders ? `${fmtRs(link.sales_rs)} · ${split(link)}` : "Customers who tapped Details and ordered"}
        />
        <StatCard label="COD orders (button)" value={fmtInt(link.cod?.orders ?? 0)} note={`${worth(link.cod)} · cash on delivery`} />
        <StatCard label="Prepaid orders (button)" value={fmtInt(link.prepaid?.orders ?? 0)} note={`${worth(link.prepaid)} · paid online`} />
        <StatCard
          label="Orders after a talk"
          value={fmtInt(w.orders)}
          note={w.orders ? `${fmtRs(w.sales_rs)} · ${split(w)} · by phone` : "Talked with the agent, then ordered"}
        />
      </div>
    </div>
  );
}

const plural = (n: number, word: string) => `${fmtInt(n)} ${word}${n === 1 ? "" : "s"}`;

/** Rupees with paise while small (₹0.80), whole rupees once large (₹1,240). */
const rs = (n: number) => fmtRs(n, n < 100 ? 2 : 0);
