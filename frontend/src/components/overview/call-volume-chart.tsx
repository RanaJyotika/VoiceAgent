"use client";

import { useState } from "react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card } from "@/components/ui/card";
import type { SeriesPoint } from "@/lib/api";
import { fmtInt } from "@/lib/format";
import { cn } from "@/lib/utils";

const SERIES = [
  { key: "calls", label: "Total calls", color: "var(--brand)" },
  { key: "connected", label: "Connected", color: "var(--info)" },
  { key: "conversions", label: "Conversions", color: "var(--good)" },
  { key: "failed", label: "Failed", color: "var(--bad)" },
] as const;

type Key = (typeof SERIES)[number]["key"];

export function CallVolumeChart({ series, bucket }: { series: SeriesPoint[]; bucket: "hour" | "day" }) {
  const [key, setKey] = useState<Key>("calls");
  const s = SERIES.find((x) => x.key === key)!;
  const empty = series.every((p) => p[key] === 0);

  return (
    <Card className="flex min-w-0 flex-col">
      <div className="flex flex-wrap items-center gap-3 border-b px-4 py-2.5">
        <h2 className="text-[15px] font-medium">Call volume</h2>
        <div className="ml-auto flex flex-wrap items-center gap-1" role="tablist">
          {SERIES.map((x) => (
            <button
              key={x.key}
              type="button"
              role="tab"
              aria-selected={key === x.key}
              onClick={() => setKey(x.key)}
              className={cn(
                "h-7 rounded-md px-2.5 text-[13px] font-medium transition-colors",
                key === x.key ? "bg-muted text-foreground" : "text-foreground/75 hover:text-foreground",
              )}
            >
              {x.label}
            </button>
          ))}
        </div>
        <span className="rounded-md border border-input px-2.5 py-1 text-[12.5px] text-muted-foreground">{bucket === "hour" ? "Hourly" : "Daily"}</span>
      </div>
      <div className="relative h-[280px] px-2 pt-4 pb-2">
        {empty && <div className="absolute inset-0 grid place-items-center text-muted-foreground">No {s.label.toLowerCase()} in this period.</div>}
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={series} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={s.color} stopOpacity={0.18} />
                <stop offset="100%" stopColor={s.color} stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="var(--border)" />
            <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} minTickGap={24} />
            <YAxis tickLine={false} axisLine={false} tick={{ fill: "var(--muted-foreground)", fontSize: 12 }} width={44} allowDecimals={false} />
            <Tooltip
              cursor={{ stroke: "var(--input)" }}
              contentStyle={{ background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 13 }}
              labelStyle={{ color: "var(--muted-foreground)" }}
              formatter={(v) => [fmtInt(Number(v)), s.label]}
            />
            <Area type="monotone" dataKey={key} stroke={s.color} strokeWidth={2} fill="url(#fill)" isAnimationActive={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
