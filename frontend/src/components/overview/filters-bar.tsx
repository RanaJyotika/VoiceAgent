"use client";

import { Select } from "@/components/ui/select";
import type { Campaign, OverviewQuery, Range } from "@/lib/api";
import { cn } from "@/lib/utils";

const RANGES: { key: Range; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "7d" },
  { key: "30d", label: "30d" },
  { key: "custom", label: "Custom" },
];

const today = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD, local date

type Props = { query: OverviewQuery; campaigns: Campaign[]; onChange: (q: OverviewQuery) => void };

export function FiltersBar({ query, campaigns, onChange }: Props) {
  const dateCls =
    "h-8 rounded-md border border-input bg-card px-2.5 text-[13px] outline-none focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20";
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="Period">
        {RANGES.map((r) => (
          <button
            key={r.key}
            type="button"
            aria-pressed={query.range === r.key}
            onClick={() =>
              onChange({
                ...query,
                range: r.key,
                dayFrom: r.key === "custom" ? (query.dayFrom ?? today()) : undefined,
                dayTo: r.key === "custom" ? (query.dayTo ?? today()) : undefined,
              })
            }
            className={cn(
              "h-8 rounded-md border border-input px-3 text-[13px] font-medium transition-colors",
              query.range === r.key ? "bg-muted text-foreground" : "bg-card text-foreground/85 hover:bg-muted",
            )}
          >
            {r.label}
          </button>
        ))}
      </div>

      {query.range === "custom" && (
        <div className="flex items-center gap-1.5 text-[13px] text-muted-foreground">
          <input
            type="date"
            aria-label="From"
            className={dateCls}
            value={query.dayFrom ?? ""}
            max={query.dayTo}
            onChange={(e) => onChange({ ...query, dayFrom: e.target.value })}
          />
          to
          <input
            type="date"
            aria-label="To"
            className={dateCls}
            value={query.dayTo ?? ""}
            min={query.dayFrom}
            max={today()}
            onChange={(e) => onChange({ ...query, dayTo: e.target.value })}
          />
        </div>
      )}

      <Select
        aria-label="Campaign"
        className="w-[190px]"
        value={query.campaignId ?? 0}
        onChange={(e) => onChange({ ...query, campaignId: Number(e.target.value) || undefined })}
      >
        <option value={0}>All campaigns</option>
        {campaigns.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </Select>
    </div>
  );
}
