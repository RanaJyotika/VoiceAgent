"use client";

import { useState, useSyncExternalStore } from "react";
import { AlertTriangle, Phone, Plus, RefreshCw } from "lucide-react";
import { Card } from "@/components/ui/card";
import Link from "next/link";
import { Button, buttonClass } from "@/components/ui/button";
import { useMakeCall } from "@/components/make-call/make-call-provider";
import { usePoll } from "@/hooks/use-poll";
import { overviewUrl, type Overview, type OverviewQuery } from "@/lib/api";
import { fmtInt, fmtPct, fmtRs, fmtToday, pct } from "@/lib/format";
import { AttentionList } from "./attention-list";
import { CallVolumeChart } from "./call-volume-chart";
import { FiltersBar } from "./filters-bar";
import { LiveCallsTable } from "./live-calls-table";
import { StatCard, change } from "./stat-card";
import { TodayCards } from "./today-cards";

const VS: Record<string, string> = { today: "vs yesterday", yesterday: "vs day before", "7d": "vs prev 7d", "30d": "vs prev 30d", custom: "vs period before" };
const noSubscribe = () => () => {};

export function OverviewView() {
  const [query, setQuery] = useState<OverviewQuery>({ range: "today" });
  const { openMakeCall } = useMakeCall();
  // Today's date is known only in the browser (the page itself is built ahead of time).
  const today = useSyncExternalStore(noSubscribe, fmtToday, () => "");

  const customReady = query.range !== "custom" || (query.dayFrom && query.dayTo);
  const { data, error, loading } = usePoll<Overview>(customReady ? overviewUrl(query) : null, 15_000);
  const n = data?.now;
  const b = data?.before;
  const vs = VS[query.range];

  return (
    <div className="pb-10">
      <div className="border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0">
            <h1 className="text-[20px] font-semibold tracking-tight">Overview</h1>
            <p className="mt-0.5 text-[13.5px] text-muted-foreground">{today ? `${today} · Vedic Shop` : "Vedic Shop"}</p>
          </div>
          <div className="ml-auto flex gap-2">
            <Button variant="outline" onClick={openMakeCall}>
              <Phone />
              Make call
            </Button>
            <Link href="/campaigns/new/" className={buttonClass("primary")}>
              <Plus />
              Create campaign
            </Link>
          </div>
        </div>
        <div className="mt-4">
          <FiltersBar query={query} campaigns={data?.campaigns ?? []} onChange={setQuery} />
        </div>
      </div>

      <div className="space-y-5 px-4 pt-5 sm:px-6">
        {error && (
          <div className="flex items-center gap-2 rounded-lg border border-bad/30 bg-bad-soft px-4 py-3 text-[13.5px] text-bad">
            <AlertTriangle className="size-4 shrink-0" />
            Could not load the numbers: {error}. Is the Python server running?
            {loading && <RefreshCw className="ml-auto size-4 animate-spin" />}
          </div>
        )}

        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <StatCard label="Total calls" value={n ? fmtInt(n.calls) : "—"} change={n && b ? change(n.calls, b.calls) : null} note={vs} />
          <StatCard
            label="Connected"
            value={n ? fmtInt(n.connected) : "—"}
            change={n && b ? change(n.connected, b.connected) : null}
            note={n?.calls ? `${fmtPct(pct(n.connected, n.calls))} rate` : ""}
          />
          <StatCard
            label="Conversions"
            value={n ? fmtInt(n.conversions) : "—"}
            change={n && b ? change(n.conversions, b.conversions) : null}
            note={n?.connected ? `${fmtPct(pct(n.conversions, n.connected))} of connected` : ""}
          />
          <StatCard
            label="Live now"
            value={data ? fmtInt(data.live.length) : "—"}
            note={data ? (data.auto.running ? `${fmtInt(data.queued)} queued` : `Auto-calling off · ${fmtInt(data.waiting)} waiting`) : ""}
          />
          <StatCard
            label="Total spend"
            value={n ? fmtRs(n.spend_rs) : "—"}
            change={n && b ? change(n.spend_rs, b.spend_rs) : null}
            upIsBad
            note={n?.calls ? `${fmtRs(n.spend_rs / n.calls, 2)} / call` : ""}
          />
          <StatCard
            label="Failed"
            value={n ? fmtInt(n.failed) : "—"}
            change={n && b ? change(n.failed, b.failed) : null}
            upIsBad
            note={n?.calls ? `${fmtPct(pct(n.failed, n.calls))} of calls` : ""}
          />
        </div>

        <LiveCallsTable campaigns={data?.campaigns ?? []} campaignId={query.campaignId} />

        <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
          <CallVolumeChart series={data?.series ?? []} bucket={data?.bucket ?? "hour"} />
          <Card className="h-fit overflow-hidden">
            <div className="flex items-center gap-2.5 border-b px-4 py-3">
              <AlertTriangle className="size-4 text-warn" />
              <h2 className="text-[15px] font-medium">Attention required</h2>
            </div>
            {data ? <AttentionList items={data.attention} /> : <div className="px-4 py-6 text-muted-foreground">Loading…</div>}
          </Card>
        </div>

        <TodayCards />
      </div>
    </div>
  );
}
