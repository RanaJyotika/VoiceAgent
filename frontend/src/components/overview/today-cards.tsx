"use client";

import { usePoll } from "@/hooks/use-poll";
import { fmtInt, fmtPct, fmtRs, pct } from "@/lib/format";
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
};

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
    </section>
  );
}

const plural = (n: number, word: string) => `${fmtInt(n)} ${word}${n === 1 ? "" : "s"}`;

/** Rupees with paise while small (₹0.80), whole rupees once large (₹1,240). */
const rs = (n: number) => fmtRs(n, n < 100 ? 2 : 0);
