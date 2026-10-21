"use client";

import { useEffect, useState } from "react";
import { PhoneOff } from "lucide-react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { usePoll } from "@/hooks/use-poll";
import type { Campaign, LiveCall } from "@/lib/api";
import { fmtClock, fmtPhone, fmtTime } from "@/lib/format";
import { classic } from "@/lib/nav";

const STAGE: Record<string, { label: string; tone: Tone }> = {
  ringing: { label: "Ringing", tone: "warn" },
  agent_speaking: { label: "Agent speaking", tone: "info" },
  listening: { label: "Listening", tone: "info" },
  ending: { label: "Ending", tone: "neutral" },
};

/** Ringing and in-progress calls, refreshed every 2 s; the clocks tick every second in between. */
export function LiveCallsTable({ campaigns, campaignId }: { campaigns: Campaign[]; campaignId?: number }) {
  const { data, updatedAt } = usePoll<LiveCall[]>("/api/live", 2_000);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const names = new Map(campaigns.map((c) => [c.id, c.name]));
  const calls = (data ?? []).filter((c) => !campaignId || c.campaign_id === campaignId);
  const drift = Math.max(0, (now - updatedAt) / 1000); // the clocks count on from the last poll

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="relative flex size-2.5">
          {calls.length > 0 && <span className="absolute inline-flex size-full animate-ping rounded-full bg-good opacity-60" />}
          <span className={`relative inline-flex size-2.5 rounded-full ${calls.length ? "bg-good" : "bg-subtle-foreground"}`} />
        </span>
        <div>
          <h2 className="text-[15px] font-medium">Live calls</h2>
          <div className="text-[12.5px] text-muted-foreground">Updating in real time</div>
        </div>
        <a href={classic("history")} className="ml-auto text-[13px] font-medium hover:underline">
          All calls
        </a>
      </div>

      {calls.length === 0 ? (
        // Outside the table, so it stays in view on a phone where the table scrolls sideways.
        <div className="border-t px-4 py-10 text-center text-muted-foreground">
          <PhoneOff className="mx-auto mb-2 size-5 opacity-60" />
          No calls right now.
        </div>
      ) : (
        <div className="overflow-x-auto border-t">
          <table className="w-full min-w-[860px] text-left text-[13.5px]">
            <thead className="text-[12.5px] text-muted-foreground">
              <tr className="border-b">
                {["Customer", "Campaign", "Agent", "Order", "Started", "Duration", "Stage", "Status"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {calls.map((c) => {
                const secs = c.seconds + drift;
                const stage = STAGE[c.status] ?? { label: c.status, tone: "neutral" as Tone };
                const ringing = c.status === "ringing";
                return (
                  <tr key={c.call_id} className="hover:bg-muted/50">
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{c.customer_name || "Unknown"}</div>
                      <div className="font-mono text-[12.5px] text-muted-foreground">{fmtPhone(c.to)}</div>
                    </td>
                    <td className="px-4 py-2.5">
                      {c.campaign_id ? (names.get(c.campaign_id) ?? `Campaign ${c.campaign_id}`) : c.channel === "browser" ? "Browser test" : "Single call"}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {c.voice}
                      {c.agent ? ` — ${c.agent}` : ""}
                    </td>
                    <td className="px-4 py-2.5 font-mono text-[12.5px]">{c.order_id ? `#${c.order_id}` : "—"}</td>
                    <td className="px-4 py-2.5 text-muted-foreground tabular">{fmtTime(new Date(now - secs * 1000))}</td>
                    <td className="px-4 py-2.5 font-mono font-medium">
                      {ringing && c.ring_left != null ? `${Math.max(0, Math.round(c.ring_left - drift))}s left` : fmtClock(secs)}
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge tone={stage.tone}>{stage.label}</Badge>
                    </td>
                    <td className="px-4 py-2.5">
                      <Badge tone={ringing ? "warn" : "good"} dot>
                        {ringing ? "Ringing" : "Live"}
                      </Badge>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
