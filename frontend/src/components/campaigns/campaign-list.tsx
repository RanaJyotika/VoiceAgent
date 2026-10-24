"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { CalendarCheck, Megaphone, Package, Plus, ShoppingCart, type LucideIcon } from "lucide-react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { PAGE_SIZE, Pager, usePaged } from "@/components/ui/pager";
import { usePoll } from "@/hooks/use-poll";
import { useCallTypes } from "@/hooks/use-static";
import type { Board, CampaignStatus } from "@/lib/api";
import { fmtInt, fmtRs } from "@/lib/format";
import { cn } from "@/lib/utils";
import { campaignHref } from "./shared";

const STATUS: Record<CampaignStatus, { label: string; tone: Tone; dot?: boolean }> = {
  running: { label: "Running", tone: "info", dot: true },
  scheduled: { label: "Scheduled", tone: "warn" },
  paused: { label: "Paused", tone: "warn" },
  completed: { label: "Completed", tone: "good" },
  draft: { label: "Draft", tone: "neutral" },
};
const TABS: ("all" | CampaignStatus)[] = ["all", "running", "scheduled", "paused", "completed", "draft"];

/** "Start from a template": the kinds of call a campaign can be, each with its own picture. */
const TEMPLATE_ICONS: Record<string, LucideIcon> = {
  abandoned_cart: ShoppingCart,
  order_confirmation: Package,
  booking_confirmation: CalendarCheck,
  campaign_invite: Megaphone,
};

function Kpi({ label, value, note }: { label: string; value: number | undefined; note: string }) {
  return (
    <Card className="px-4 py-3.5">
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-[26px] leading-none font-semibold tracking-tight tabular">{value == null ? "—" : fmtInt(value)}</div>
      <div className="mt-2.5 text-[12.5px] text-muted-foreground">{note}</div>
    </Card>
  );
}

/** The Campaigns page: how many are running / waiting / done, templates, and every campaign with its numbers.
 *  The Store orders campaign has its own page and is left out, as before. */
export function CampaignList() {
  const router = useRouter();
  const { data, error } = usePoll<Board>("/api/campaigns/board", 5_000);
  const types = useCallTypes();
  const [tab, setTab] = useState<(typeof TABS)[number]>("all");

  const rows = (data?.campaigns ?? []).filter((c) => tab === "all" || c.status === tab);
  const paged = usePaged(rows, PAGE_SIZE, tab);
  const k = data?.counts;

  return (
    <div className="pb-10">
      <div className="border-b px-4 pt-5 pb-4 sm:px-6">
        <h1 className="text-[20px] font-semibold tracking-tight">Campaigns</h1>
        <p className="mt-0.5 text-[13.5px] text-muted-foreground">Run outbound calls at scale with any agent and audience.</p>
      </div>

      <div className="space-y-5 px-4 pt-5 sm:px-6">
        {error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">Could not load the campaigns: {error}</p>}

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Kpi label="Running" value={k?.running} note="Calling now" />
          <Kpi label="Scheduled" value={k?.scheduled} note={k?.paused ? `Start at a set time · ${k.paused} paused` : "Start at a set time"} />
          <Kpi label="Past campaigns" value={k?.completed} note="Finished, or past their last day" />
          <Link
            href="/campaigns/new/"
            className="group flex flex-col justify-between rounded-lg border border-dashed border-input bg-card px-4 py-3.5 transition-colors hover:border-primary hover:bg-muted/40"
          >
            <span className="text-[13px] text-muted-foreground">New campaign</span>
            <span className="mt-1.5 flex items-center gap-2.5">
              <span className="grid size-9 place-items-center rounded-md bg-primary text-primary-foreground transition-transform group-hover:scale-105">
                <Plus className="size-5" />
              </span>
              <span className="text-[15px] font-medium">Create campaign</span>
            </span>
            <span className="mt-2.5 text-[12.5px] text-muted-foreground">From an Excel sheet or typed numbers</span>
          </Link>
        </div>

        <Card className="overflow-hidden">
          <div className="border-b px-4 py-3">
            <h2 className="text-[15px] font-medium">Start from a template</h2>
            <p className="text-[12.5px] text-muted-foreground">
              Each template is a kind of call: its script, the sheet columns it reads and the results it records.
            </p>
          </div>
          <div className="grid gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
            {(types ?? []).map((t) => {
              const Icon = TEMPLATE_ICONS[t.key] ?? Megaphone;
              return (
                <Link
                  key={t.key}
                  href={`/campaigns/new/?type=${t.key}`}
                  className="rounded-lg border p-3.5 transition-colors hover:border-input hover:bg-muted/40"
                >
                  <Icon className="size-[18px] text-muted-foreground" />
                  <div className="mt-2.5 font-medium">{t.name}</div>
                  <div className="mt-0.5 line-clamp-2 text-[12.5px] text-muted-foreground">{t.description}</div>
                </Link>
              );
            })}
          </div>
        </Card>

        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
            <h2 className="text-[15px] font-medium">All campaigns</h2>
            <div className="ml-auto flex flex-wrap gap-0.5 rounded-lg bg-muted p-0.5" role="tablist">
              {TABS.map((t) => (
                <button
                  key={t}
                  type="button"
                  role="tab"
                  aria-selected={tab === t}
                  onClick={() => setTab(t)}
                  className={cn(
                    "h-7 rounded-md px-3 text-[13px] transition-colors",
                    tab === t ? "bg-card font-medium text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {t === "all" ? "All" : STATUS[t].label}
                </button>
              ))}
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1040px] text-left text-[13.5px]">
              <thead className="text-[12.5px] text-muted-foreground">
                <tr className="border-b">
                  <th className="px-4 py-2.5 font-normal">Campaign</th>
                  <th className="px-4 py-2.5 font-normal">Status</th>
                  <th className="px-4 py-2.5 font-normal">Agent</th>
                  <th className="px-4 py-2.5 text-right font-normal" title="Contacts on the list: one call each">
                    Calls
                  </th>
                  <th className="px-4 py-2.5 text-right font-normal" title="Contacts called at least once">
                    Called
                  </th>
                  <th className="px-4 py-2.5 text-right font-normal" title="Picked up and spoke with the agent">
                    Connected
                  </th>
                  <th className="px-4 py-2.5 text-right font-normal" title="No answer or busy">
                    Not connected
                  </th>
                  <th className="px-4 py-2.5 text-right font-normal" title="Picked up, then the call ended before any result">
                    Hung up
                  </th>
                  <th className="px-4 py-2.5 text-right font-normal" title="The AI agent plus the phone line, for every call so far">
                    Cost
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {paged.rows.map((c) => {
                  const st = STATUS[c.status];
                  return (
                    <tr
                      key={c.id}
                      tabIndex={0}
                      onClick={() => router.push(campaignHref(c.id))}
                      onKeyDown={(e) => e.key === "Enter" && router.push(campaignHref(c.id))}
                      className="cursor-pointer outline-none hover:bg-muted/50 focus-visible:bg-muted"
                    >
                      <td className="px-4 py-3">
                        <div className="font-medium">{c.name}</div>
                        <div className="text-[12.5px] text-muted-foreground">
                          {types?.find((t) => t.key === c.call_type)?.name ?? c.call_type}
                          {c.auto ? " · Automatic" : ""}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <Badge tone={st.tone} dot={st.dot}>
                          {st.label}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {c.agent ? `${c.voice}${c.voice ? " — " : ""}${c.agent}` : <span className="text-subtle-foreground">Not recorded</span>}
                      </td>
                      <td className="px-4 py-3 text-right tabular">{fmtInt(c.contacts)}</td>
                      <td className="px-4 py-3 text-right tabular">{fmtInt(c.called)}</td>
                      <td className="px-4 py-3 text-right font-medium tabular">{fmtInt(c.connected)}</td>
                      <td className="px-4 py-3 text-right tabular">{fmtInt(c.not_connected)}</td>
                      <td className="px-4 py-3 text-right tabular">{fmtInt(c.hung_up)}</td>
                      <td className="px-4 py-3 text-right whitespace-nowrap">
                        <div className="font-medium tabular">{fmtRs(c.cost_rs)}</div>
                        <div className="text-[12px] text-muted-foreground tabular">
                          Agent {fmtRs(c.agent_rs)} · Line {fmtRs(c.line_rs)}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {data && rows.length === 0 && (
              <p className="px-4 py-10 text-center text-muted-foreground">
                {tab === "all" ? "No campaigns yet. Create one from a template above." : `No ${STATUS[tab].label.toLowerCase()} campaigns.`}
              </p>
            )}
          </div>
          <Pager page={paged.page} pages={paged.pages} onPage={paged.setPage} total={paged.total} size={paged.size} />
        </Card>
      </div>
    </div>
  );
}
