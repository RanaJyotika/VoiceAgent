"use client";

import { useState } from "react";
import { Clock, Phone, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { usePoll } from "@/hooks/use-poll";
import { useConfig } from "@/hooks/use-static";
import { send, type CallbackList as Data, type PhoneLine } from "@/lib/api";
import { fmtOrder, fmtPhone } from "@/lib/format";
import { outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";
import { fitVoice, remembered } from "@/lib/voices";

const STATUSES = ["no_answer", "busy", "dropped", "call_later"];
const WHY: Record<string, string> = {
  no_answer: "Did not answer",
  busy: "Was busy",
  dropped: "Call ended without a result",
  call_later: "Asked to be called later",
};

/** The reason line already says it: "Call ended without a result" and "call ended without a result (customer hung up…)". */
const sameNote = (a: string, b: string) => {
  const [x, y] = [a.toLowerCase(), b.toLowerCase()];
  return x.includes(y) || y.startsWith(x);
};

const fmtWhen = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

/** A datetime-local value (local wall time, no zone) for a stored time; the browser is taken to be on Indian time. */
function toLocalInput(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** People who did not answer, were busy, dropped off or asked for a later call, from every campaign. */
export function CallbackList() {
  const { toast, confirm } = useFeedback();
  const cfg = useConfig();
  const { data, error, reload, updatedAt } = usePoll<Data>("/api/callbacks", 5_000);
  const line = usePoll<PhoneLine>("/api/plivo/status", 60_000).data;
  const [show, setShow] = useState("all");
  const [times, setTimes] = useState<Record<number, string>>({}); // a time typed but not set yet

  const rows = data?.rows ?? [];
  const shown = rows.filter((c) => show === "all" || c.status === show);
  const now = updatedAt;

  const schedule = async (id: number, at: string | null) => {
    try {
      await send(`/api/contacts/${id}/schedule`, "POST", { at });
      toast(at ? `Will call at ${fmtWhen(at)}.` : "Will call as soon as possible.");
      setTimes((all) => Object.fromEntries(Object.entries(all).filter(([k]) => Number(k) !== id)));
    } catch (e) {
      toast(`Could not set the time: ${(e as Error).message}`, true);
    }
    reload();
  };

  const giveUp = async (c: Data["rows"][number]) => {
    const ok = await confirm({
      title: `Stop calling ${c.customer_name || "this contact"}?`,
      body: "They come off this list and move to Needs review.",
      confirmLabel: "Stop calling",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`/api/contacts/${c.id}/giveup`, "POST");
      toast("Taken off the call back list.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    reload();
  };

  const callNow = async (c: Data["rows"][number]) => {
    if (!cfg) return;
    const to = line?.dial_only_my_number ? "your own phone (test mode)" : fmtPhone(c.phone) || "no phone";
    const ok = await confirm({
      title: `Call ${c.customer_name || "this contact"} now?`,
      body: (
        <>
          Number: {to}
          <br />
          Agent: {cfg.model_labels[cfg.model] ?? cfg.model}. This is a real, paid phone call.
        </>
      ),
      confirmLabel: "Call now",
    });
    if (!ok) return;
    try {
      const voice = fitVoice(cfg, cfg.model, remembered("voice") || cfg.voice);
      const qs = new URLSearchParams({ voice, model: cfg.model, language: "", build: cfg.build });
      const j = await send<{ dialed: string }>(`/api/calls/dial/${c.id}?${qs}`, "POST");
      toast(`Dialing ${fmtPhone(j.dialed)}… Follow it in Live calls.`);
    } catch (e) {
      toast(`Could not dial: ${(e as Error).message}`, true);
    }
    reload();
  };

  const allDueNow = async () => {
    const ok = await confirm({
      title: `Make all ${rows.length} due right now?`,
      body: "Their campaigns ring them next, after anyone still to be called for the first time.",
      confirmLabel: "Make all due now",
    });
    if (!ok) return;
    await Promise.all(rows.map((c) => send(`/api/contacts/${c.id}/schedule`, "POST", { at: null }).catch(() => null)));
    toast("All due now.");
    reload();
  };

  /** Give up on everyone shown (all, or the chip picked): they leave this list for Needs review. */
  const giveUpAll = async () => {
    const which = show === "all" ? "" : ` (${outcomeOf(show)?.label.toLowerCase() ?? show})`;
    const ok = await confirm({
      title: `Stop calling all ${shown.length}${which}?`,
      body: "They all come off the call back list and move to Needs review. No campaign rings them again unless they are put back.",
      confirmLabel: `Stop calling ${shown.length}`,
      danger: true,
    });
    if (!ok) return;
    try {
      const j = await send<{ given_up: number }>("/api/callbacks/giveup", "POST", { ids: shown.map((c) => c.id) });
      toast(`${j.given_up} taken off the call back list.`);
      setShow("all");
    } catch (e) {
      toast((e as Error).message, true);
    }
    reload();
  };

  return (
    <div className="pb-10">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <div className="min-w-0 max-w-3xl">
          <h1 className="text-[20px] font-semibold tracking-tight">Call back</h1>
          <p className="mt-0.5 text-[13.5px] text-muted-foreground">
            People who did not answer, were busy, dropped off, or asked to be called later, from every campaign. A running campaign rings its own when their
            time comes.
          </p>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <Button variant="outline" onClick={allDueNow} disabled={!rows.length}>
            <Clock />
            Make all due now
          </Button>
          <Button variant="outline" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={giveUpAll} disabled={!shown.length}>
            <X />
            {show === "all" ? "Give up on all" : `Give up on all ${outcomeOf(show)?.label.toLowerCase() ?? show}`}
          </Button>
        </div>
      </div>

      <div className="space-y-4 px-4 pt-5 sm:px-6">
        <div className="flex flex-wrap gap-1.5">
          {["all", ...STATUSES.filter((s) => rows.some((c) => c.status === s))].map((k) => (
            <button
              key={k}
              type="button"
              aria-pressed={show === k}
              onClick={() => setShow(k)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[12.5px]",
                show === k ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
              )}
            >
              {k === "all" ? "All" : (outcomeOf(k)?.label ?? k)}
              <b className="tabular">{k === "all" ? rows.length : rows.filter((c) => c.status === k).length}</b>
            </button>
          ))}
        </div>

        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[1040px] text-left text-[13.5px]">
              <thead className="text-[12.5px] text-muted-foreground">
                <tr className="border-b">
                  {["Customer", "Campaign", "Why", "Tries", "Last time", "Next call", ""].map((h, i) => (
                    <th key={i} className={cn("px-4 py-2.5 font-normal", h === "Tries" && "text-right")}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {shown.map((c) => {
                  const d = c.last_details || {};
                  const st = outcomeOf(c.status);
                  const extra =
                    c.status === "call_later" && d.callback_time
                      ? `“${d.callback_time}”`
                      : c.last_result &&
                          ![WHY[c.status] ?? "", outcomeOf(c.status)?.label ?? "", c.status.replace(/_/g, " ")].some((w) => w && sameNote(w, c.last_result!))
                        ? c.last_result
                        : "";
                  const due = !c.next_try_at || new Date(c.next_try_at).getTime() <= now;
                  const exhausted = c.status !== "call_later" && (c.attempts || 0) >= (data?.max_attempts ?? 3);
                  const typed = times[c.id];
                  return (
                    <tr key={c.id} className="align-top hover:bg-muted/40">
                      <td className="px-4 py-3">
                        <div className="font-medium">{c.customer_name || "—"}</div>
                        <div className="font-mono text-[12px] text-muted-foreground">
                          {[c.phone ? fmtPhone(c.phone) : "no phone", fmtOrder(c.order_id)].filter(Boolean).join(" · ")}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-[12.5px] text-muted-foreground">{c.campaign_name || "Single call"}</td>
                      <td className="max-w-80 min-w-56 px-4 py-3">
                        {st && <Badge tone={st.tone}>{st.label}</Badge>}
                        <div className="mt-1 text-[12px] leading-snug text-muted-foreground">
                          {WHY[c.status] ?? c.status}
                          {extra ? ` · ${extra}` : ""}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right tabular">
                        {c.attempts || 0}
                        {exhausted && <div className="text-[11.5px] text-muted-foreground">no more automatic tries</div>}
                      </td>
                      <td className="px-4 py-3 text-[12.5px] whitespace-nowrap text-muted-foreground">{fmtWhen(c.last_call_at) || "—"}</td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <input
                            type="datetime-local"
                            aria-label="Next call"
                            className="h-8 rounded-md border border-input bg-card px-2 text-[12.5px]"
                            value={typed ?? toLocalInput(c.next_try_at)}
                            onChange={(e) => setTimes({ ...times, [c.id]: e.target.value })}
                          />
                          <Button size="sm" variant="outline" disabled={typed === undefined} onClick={() => schedule(c.id, typed || null)}>
                            Set
                          </Button>
                          {!due && (
                            <Button size="sm" variant="ghost" onClick={() => schedule(c.id, null)} title="Call as soon as possible">
                              ASAP
                            </Button>
                          )}
                        </div>
                        {due && <div className="mt-1 text-[12px] font-medium text-warn">due now</div>}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex justify-end gap-1">
                          <Button
                            size="sm"
                            variant="ghost"
                            onClick={() => callNow(c)}
                            disabled={!cfg || !line?.ready}
                            title={line?.ready ? "A real, paid call" : "The phone line is off"}
                          >
                            <Phone />
                            Call now
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="text-bad hover:bg-bad-soft"
                            onClick={() => giveUp(c)}
                            title="Take off this list; goes to Needs review"
                          >
                            <X />
                            Give up
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {data && shown.length === 0 && <p className="px-4 py-10 text-center text-muted-foreground">Nobody is waiting for a call back.</p>}
            {!data && <p className="px-4 py-10 text-center text-muted-foreground">{error ? `Could not load the list: ${error}` : "Loading…"}</p>}
          </div>
        </Card>
        {rows.length > 0 && (
          <p className="text-[12.5px] text-muted-foreground">
            {data?.running
              ? "A campaign is running: its own contacts here are rung when their time comes."
              : "Start their campaign (Campaigns) to ring these when their time comes, or press Call now on one."}
          </p>
        )}
      </div>
    </div>
  );
}
