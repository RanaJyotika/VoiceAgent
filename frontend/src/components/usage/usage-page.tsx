"use client";

import { useState } from "react";
import { Area, AreaChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Save } from "lucide-react";
import { StatCard } from "@/components/overview/stat-card";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { usePoll } from "@/hooks/use-poll";
import { send } from "@/lib/api";
import { fmtInt, fmtRs } from "@/lib/format";
import { cn } from "@/lib/utils";

type Range = "month" | "last_month" | "7d" | "30d";
const RANGES: { key: Range; label: string }[] = [
  { key: "month", label: "This month" },
  { key: "last_month", label: "Last month" },
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
];

type Budget = { monthly_rs: number; daily_rs: number; alert_at: number[]; pause: boolean; spent_today_rs: number; spent_month_rs: number };
type Usage = {
  label: string;
  from: string;
  to: string;
  spend_rs: number;
  spend_change: number | null;
  projected_rs: number | null;
  minutes: number;
  minutes_change: number | null;
  per_min_rs: number;
  per_min_change: number | null;
  calls: number;
  series: ({ day: string; label: string } & Record<string, number | string>)[];
  providers: { key: string; label: string; calls: number; minutes: number; cost_rs: number }[];
  provider_labels: Record<string, string>;
  campaigns: { id: number; name: string; calls: number; cost_rs: number }[];
  agents: { name: string; calls: number; cost_rs: number }[];
  budget: Budget;
};

/** One colour per provider in the cost trend, readable in light and dark. */
const COLORS: Record<string, string> = {
  sarvam: "var(--brand)",
  openai: "var(--info)",
  gemini: "var(--warn)",
  unknown: "var(--subtle-foreground)",
  plivo: "var(--good)",
};
const rs = (n: number) => fmtRs(n, n < 100 ? 2 : 0);
const day = (d: string) => new Date(`${d}T00:00:00`).toLocaleDateString("en-IN", { day: "numeric", month: "short" });

/** Usage: what calling cost (by day, provider, campaign, agent) and the workspace budget, which really stops calling. */
export function UsagePage() {
  const [range, setRange] = useState<Range>("month");
  const { data: u, error, reload } = usePoll<Usage>(`/api/usage?range=${range}`, 30_000);

  return (
    <div className="pb-10">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="min-w-0">
          <h1 className="text-[20px] font-semibold tracking-tight">Usage</h1>
          <p className="mt-0.5 text-[13.5px] text-muted-foreground">
            {u ? `${u.label[0].toUpperCase()}${u.label.slice(1)} · ${day(u.from)} – ${day(u.to)}` : "What calling costs, and your limits"}
          </p>
        </div>
        <div className="ml-auto flex rounded-lg bg-muted p-0.5" role="tablist">
          {RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              role="tab"
              aria-selected={range === r.key}
              onClick={() => setRange(r.key)}
              className={cn(
                "h-8 rounded-md px-3 text-[13px] transition-colors",
                range === r.key ? "bg-card font-medium text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
              )}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-4 px-4 pt-5 sm:px-6">
        {error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">Could not load usage: {error}</p>}

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard label="Spend this period" value={u ? rs(u.spend_rs) : "—"} change={u?.spend_change} upIsBad note={u ? `${fmtInt(u.calls)} calls` : ""} />
          <StatCard
            label="Projected month-end"
            value={u ? (u.projected_rs != null ? rs(u.projected_rs) : "—") : "—"}
            note={u?.projected_rs != null ? "At this month's pace so far" : "For this month only"}
          />
          <StatCard label="Call minutes" value={u ? fmtInt(Math.round(u.minutes)) : "—"} change={u?.minutes_change} note="Every call, tests included" />
          <StatCard label="Avg cost / min" value={u ? rs(u.per_min_rs) : "—"} change={u?.per_min_change} upIsBad note="Agent and phone line" />
        </div>

        {u && <BudgetBar b={u.budget} />}

        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
          <Card className="flex min-w-0 flex-col overflow-hidden">
            <div className="border-b px-4 py-3">
              <h2 className="text-[15px] font-medium">Cost trend by provider</h2>
            </div>
            <div className="h-[300px] px-2 pt-4 pb-2">
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={u?.series ?? []} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
                  <CartesianGrid vertical={false} stroke="var(--border)" />
                  <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={18} />
                  <YAxis tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={48} tickFormatter={(v) => `₹${v}`} />
                  <Tooltip
                    cursor={{ stroke: "var(--input)" }}
                    contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13 }}
                    labelStyle={{ color: "var(--muted-foreground)" }}
                    formatter={(v, name) => [rs(Number(v)), u?.provider_labels[String(name)] ?? String(name)]}
                  />
                  <Legend formatter={(name) => <span className="text-[12px] text-muted-foreground">{u?.provider_labels[String(name)] ?? name}</span>} />
                  {Object.keys(COLORS)
                    .filter((k) => u?.providers.some((p) => p.key === k))
                    .map((k) => (
                      <Area
                        key={k}
                        type="monotone"
                        dataKey={k}
                        stackId="cost"
                        stroke={COLORS[k]}
                        fill={COLORS[k]}
                        fillOpacity={0.18}
                        strokeWidth={1.5}
                        isAnimationActive={false}
                      />
                    ))}
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </Card>

          <Card className="overflow-hidden">
            <div className="border-b px-4 py-3">
              <h2 className="text-[15px] font-medium">Provider breakdown</h2>
            </div>
            <Table
              head={["Provider", "Usage", "Cost"]}
              rows={(u?.providers ?? []).map((p) => [p.label, `${fmtInt(Math.round(p.minutes))} min · ${fmtInt(p.calls)} calls`, rs(p.cost_rs)])}
              empty="No calls in this period."
            />
          </Card>
        </div>

        <div className="grid gap-4 xl:grid-cols-2">
          <Card className="overflow-hidden">
            <div className="border-b px-4 py-3">
              <h2 className="text-[15px] font-medium">Cost by campaign</h2>
            </div>
            <Table
              head={["Campaign", "Calls", "Cost"]}
              rows={(u?.campaigns ?? []).map((c) => [c.name, fmtInt(c.calls), rs(c.cost_rs)])}
              empty="No calls in this period."
            />
          </Card>
          <Card className="overflow-hidden">
            <div className="border-b px-4 py-3">
              <h2 className="text-[15px] font-medium">Cost by agent</h2>
            </div>
            <Table
              head={["Agent", "Calls", "Spend"]}
              rows={(u?.agents ?? []).map((a) => [a.name, fmtInt(a.calls), rs(a.cost_rs)])}
              empty="No calls in this period."
            />
          </Card>
        </div>

        {u && (
          <BudgetControls key={JSON.stringify(u.budget.alert_at) + u.budget.monthly_rs + u.budget.daily_rs + u.budget.pause} b={u.budget} onSaved={reload} />
        )}
      </div>
    </div>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: string[][]; empty: string }) {
  return (
    <table className="w-full text-left text-[13.5px]">
      <thead className="text-[12.5px] text-muted-foreground">
        <tr className="border-b">
          {head.map((h, i) => (
            <th key={h} className={cn("px-4 py-2.5 font-normal", i > 0 && "text-right")}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y">
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((cell, j) => (
              <td key={j} className={cn("px-4 py-2.5", j === 0 ? "font-medium" : "text-right tabular", j === r.length - 1 && "font-medium")}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
        {rows.length === 0 && (
          <tr>
            <td colSpan={head.length} className="px-4 py-6 text-center text-muted-foreground">
              {empty}
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

/** The reference's "Credits" bar: here, spend against the monthly limit (and today against the daily one). */
function BudgetBar({ b }: { b: Budget }) {
  const lines = [
    b.monthly_rs ? { name: "This month", spent: b.spent_month_rs, limit: b.monthly_rs } : null,
    b.daily_rs ? { name: "Today", spent: b.spent_today_rs, limit: b.daily_rs } : null,
  ].filter(Boolean) as { name: string; spent: number; limit: number }[];
  return (
    <Card className="overflow-hidden">
      <div className="border-b px-4 py-3">
        <h2 className="text-[15px] font-medium">Budget</h2>
      </div>
      <div className="space-y-4 p-4">
        {lines.length === 0 && (
          <p className="text-[13.5px] text-muted-foreground">
            No limits set: {rs(b.spent_month_rs)} spent this month, {rs(b.spent_today_rs)} today. Set limits in Budget controls below.
          </p>
        )}
        {lines.map((l) => {
          const share = (100 * l.spent) / l.limit;
          return (
            <div key={l.name}>
              <div className="mb-1.5 flex items-baseline justify-between text-[13px]">
                <span>{l.name}</span>
                <span className={cn("font-medium tabular", share >= 100 ? "text-bad" : share >= Math.min(...b.alert_at, 100) ? "text-warn" : "")}>
                  {Math.round(share)}%
                </span>
              </div>
              <div className="relative h-2 overflow-hidden rounded-full bg-muted">
                <div className={cn("h-full rounded-full", share >= 100 ? "bg-bad" : "bg-brand")} style={{ width: `${Math.min(100, share)}%` }} />
                {b.alert_at.map((a) => (
                  <span key={a} className="absolute top-0 h-full w-px bg-foreground/30" style={{ left: `${a}%` }} title={`Alert at ${a}%`} />
                ))}
              </div>
              <div className="mt-1.5 text-[12.5px] text-muted-foreground tabular">
                {rs(l.spent)} of {fmtRs(l.limit)}
                {b.alert_at.length ? ` · alerts at ${b.alert_at.join("%, ")}%` : ""}
                {b.pause ? " · campaigns pause at the limit" : " · campaigns keep calling past it"}
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

const ALERT_CHOICES = [50, 75, 80, 90, 100];

function BudgetControls({ b, onSaved }: { b: Budget; onSaved: () => void }) {
  const { toast } = useFeedback();
  const [monthly, setMonthly] = useState(String(b.monthly_rs || ""));
  const [daily, setDaily] = useState(String(b.daily_rs || ""));
  const [alertAt, setAlertAt] = useState<number[]>(b.alert_at);
  const [pause, setPause] = useState(b.pause);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await send("/api/budget", "PUT", { monthly_rs: Number(monthly) || 0, daily_rs: Number(daily) || 0, alert_at: alertAt, pause });
      toast("Budget saved.");
      onSaved();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="overflow-hidden">
      <div className="border-b px-4 py-3">
        <h2 className="text-[15px] font-medium">Budget controls</h2>
        <p className="text-[12.5px] text-muted-foreground">Spend counts every call, agent and phone line. Empty or 0: no limit.</p>
      </div>
      <div className="space-y-4 p-4">
        <div className="grid gap-4 sm:grid-cols-3">
          <Field label="Monthly workspace limit (₹)">
            <input className={inputClass} type="number" min={0} placeholder="No limit" value={monthly} onChange={(e) => setMonthly(e.target.value)} />
          </Field>
          <Field label="Daily limit (₹)">
            <input className={inputClass} type="number" min={0} placeholder="No limit" value={daily} onChange={(e) => setDaily(e.target.value)} />
          </Field>
          <div>
            <div className="mb-1.5 text-[13.5px] font-medium">Alert at</div>
            <div className="flex h-10 overflow-hidden rounded-md border border-input">
              {ALERT_CHOICES.map((a) => {
                const on = alertAt.includes(a);
                return (
                  <button
                    key={a}
                    type="button"
                    aria-pressed={on}
                    onClick={() => setAlertAt(on ? alertAt.filter((x) => x !== a) : [...alertAt, a].sort((x, y) => x - y))}
                    className={cn(
                      "flex-1 border-r text-[13px] last:border-r-0",
                      on ? "bg-primary text-primary-foreground" : "bg-card text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {a}%
                  </button>
                );
              })}
            </div>
            <p className="mt-1 text-[12px] text-muted-foreground">Shown in Attention required on the Overview.</p>
          </div>
        </div>
        <label className="flex cursor-pointer items-center gap-3 rounded-md border px-3 py-3 text-[13.5px]">
          <span className="flex-1">
            Pause all campaigns when a limit is reached
            <span className="block text-[12px] text-muted-foreground">
              Running campaigns stop, automatic calling waits, and Start is refused until the next day or month. Calls you place yourself still go.
            </span>
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={pause}
            onClick={() => setPause(!pause)}
            className={cn("relative h-6 w-11 shrink-0 rounded-full transition-colors", pause ? "bg-primary" : "bg-input")}
          >
            <span className={cn("absolute top-0.5 size-5 rounded-full bg-card shadow transition-[left]", pause ? "left-5.5" : "left-0.5")} />
          </button>
        </label>
        <div className="flex justify-end">
          <Button variant="primary" onClick={save} disabled={saving}>
            <Save />
            {saving ? "Saving…" : "Save budget"}
          </Button>
        </div>
      </div>
    </Card>
  );
}
