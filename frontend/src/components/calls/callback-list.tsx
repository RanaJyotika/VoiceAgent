"use client";

import { useState } from "react";
import { Clock, MessageSquare, Phone, X } from "lucide-react";
import { CallPanel } from "@/components/overview/call-panel";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { useFeedback } from "@/components/ui/feedback";
import { PAGE_SIZE, Pager, usePaged } from "@/components/ui/pager";
import { usePoll } from "@/hooks/use-poll";
import { useConfig } from "@/hooks/use-static";
import { send, type CallbackList as Data, type CallbackRow, type CallbackState, type PhoneLine } from "@/lib/api";
import { fmtOrder, fmtPhone } from "@/lib/format";
import { outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";
import { fitVoice, remembered } from "@/lib/voices";

const WHY: Record<string, string> = {
  no_answer: "Did not answer",
  busy: "Was busy",
  dropped: "Call ended without a result",
  call_later: "Asked to be called later",
};

/** Where a call-back stands (owner, 23 Oct 2026: the page did not say whether one was done, still to come, or when). */
const STATE: Record<CallbackState, { label: string; tone: Tone; chip: string }> = {
  scheduled: { label: "Scheduled", tone: "info", chip: "Scheduled" },
  due: { label: "Due now", tone: "warn", chip: "Due now" },
  not_rung: { label: "Pending", tone: "warn", chip: "Pending, not being rung" },
  no_tries: { label: "No tries left", tone: "neutral", chip: "No tries left" },
  calling: { label: "Calling now", tone: "info", chip: "Calling now" },
  done: { label: "Done", tone: "good", chip: "Done (30 days)" },
};
const WAITING: CallbackState[] = ["scheduled", "due", "not_rung", "no_tries"];

/** The reason line already says it: "Call ended without a result" and "call ended without a result (customer hung up…)". */
const sameNote = (a: string, b: string) => {
  const [x, y] = [a.toLowerCase(), b.toLowerCase()];
  return x.includes(y) || y.startsWith(x);
};

const fmtWhen = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "";

/** "in 40 min", "in 3 h", "in 2 days" */
function fromNow(iso: string, now: number) {
  const min = Math.round((new Date(iso).getTime() - now) / 60_000);
  if (min < 60) return `in ${Math.max(1, min)} min`;
  if (min < 48 * 60) return `in ${Math.round(min / 60)} h`;
  return `in ${Math.round(min / 1440)} days`;
}

/** A datetime-local value (local wall time, no zone) for a stored time; the browser is taken to be on Indian time. */
function toLocalInput(iso: string | null | undefined) {
  if (!iso) return "";
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** Why the last call put them on the list, with the customer's own words or the call's note when they add something. */
function reason(c: CallbackRow) {
  const status = c.status;
  const d = c.last_details || {};
  const extra =
    status === "call_later" && d.callback_time
      ? `“${d.callback_time}”`
      : c.last_result && ![WHY[status] ?? "", outcomeOf(status)?.label ?? "", status.replace(/_/g, " ")].some((w) => w && sameNote(w, c.last_result!))
        ? c.last_result
        : "";
  return `${WHY[status] ?? status.replace(/_/g, " ")}${extra ? ` · ${extra}` : ""}`;
}

/** The Call back page: people who did not answer, were busy, dropped off or asked for a later call, from every
 *  campaign; and what became of them. */
export function CallbackList() {
  return (
    <div className="pb-10">
      <div className="border-b px-4 pt-5 pb-4 sm:px-6">
        <h1 className="text-[20px] font-semibold tracking-tight">Call back</h1>
        <p className="mt-0.5 max-w-3xl text-[13.5px] text-muted-foreground">
          People who did not answer, were busy, dropped off, or asked to be called later, from every campaign: when they are rung again, and what came of it. A
          running campaign rings its own when their time comes.
        </p>
      </div>
      <div className="px-4 pt-5 sm:px-6">
        <CallbackBoard />
      </div>
    </div>
  );
}

/** The call-back list with its state chips: every campaign's (the Call back page), or one campaign's (its Call back tab,
 *  owner 23 Oct 2026), which leaves out the Campaign column. */
export function CallbackBoard({ campaignId }: { campaignId?: number }) {
  const { toast, confirm } = useFeedback();
  const cfg = useConfig();
  const { data, error, reload, updatedAt } = usePoll<Data>(campaignId ? `/api/callbacks?campaign_id=${campaignId}` : "/api/callbacks", 5_000);
  const line = usePoll<PhoneLine>("/api/plivo/status", 60_000).data;
  const [show, setShow] = useState<"waiting" | CallbackState>("waiting");
  const [times, setTimes] = useState<Record<number, string>>({}); // a time typed but not set yet
  const [openCall, setOpenCall] = useState<number | null>(null);

  const rows = data?.rows ?? [];
  const moved = data?.moved_on ?? [];
  const count = (s: CallbackState) => (WAITING.includes(s) ? rows : moved).filter((c) => c.state === s).length;
  const shown = show === "waiting" ? rows : (WAITING.includes(show) ? rows : moved).filter((c) => c.state === show);
  const history = show === "done" || show === "calling";
  const paged = usePaged(shown, PAGE_SIZE, show);
  const now = updatedAt;
  // "Make due now" does nothing for a contact out of tries: no campaign rings it again by itself.
  const canHurry = rows.filter((c) => c.state === "scheduled" || c.state === "not_rung");

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

  const giveUp = async (c: CallbackRow) => {
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

  const callNow = async (c: CallbackRow) => {
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
      title: `Make ${canHurry.length} due right now?`,
      body: "Everyone scheduled or pending with tries left. Their campaigns ring them next, after anyone still to be called for the first time.",
      confirmLabel: "Make due now",
    });
    if (!ok) return;
    await Promise.all(canHurry.map((c) => send(`/api/contacts/${c.id}/schedule`, "POST", { at: null }).catch(() => null)));
    toast("All due now.");
    reload();
  };

  /** Give up on everyone shown (all waiting, or the chip picked): they leave this list for Needs review. */
  const giveUpAll = async () => {
    const which = show === "waiting" ? "" : ` (${STATE[show].label.toLowerCase()})`;
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
      setShow("waiting");
    } catch (e) {
      toast((e as Error).message, true);
    }
    reload();
  };

  const chip = (key: "waiting" | CallbackState, label: string, n: number) => (
    <button
      key={key}
      type="button"
      aria-pressed={show === key}
      onClick={() => setShow(key)}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1 text-[12.5px]",
        show === key ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
      )}
    >
      {label}
      <b className="tabular">{n}</b>
    </button>
  );

  const heads = (
    history
      ? ["Customer", "Campaign", "Rescheduled because", "Tries", "Called back", "Result", ""]
      : ["Customer", "Campaign", "Why", "Tries", "Last time", "Status", "Next call", ""]
  ).filter((h) => !campaignId || h !== "Campaign");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-1.5">
        {chip("waiting", "All waiting", rows.length)}
        {WAITING.filter((s) => count(s)).map((s) => chip(s, STATE[s].chip, count(s)))}
        <span className="mx-1 h-5 w-px bg-border" aria-hidden />
        {count("calling") > 0 && chip("calling", STATE.calling.chip, count("calling"))}
        {chip("done", STATE.done.chip, count("done"))}
        {!history && (
          <div className="ml-auto flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={allDueNow} disabled={!canHurry.length}>
              <Clock />
              Make all due now
            </Button>
            <Button variant="outline" size="sm" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={giveUpAll} disabled={!shown.length}>
              <X />
              {show === "waiting" ? "Give up on all" : `Give up on all ${STATE[show].label.toLowerCase()}`}
            </Button>
          </div>
        )}
      </div>

      <Card className="overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1100px] text-left text-[13.5px]">
            <thead className="text-[12.5px] text-muted-foreground">
              <tr className="border-b">
                {heads.map((h, i) => (
                  <th key={i} className={cn("px-4 py-2.5 font-normal", h === "Tries" && "text-right")}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {paged.rows.map((c) => {
                const st = STATE[c.state];
                const customer = (
                  <td className="px-4 py-3">
                    <div className="font-medium">{c.customer_name || "—"}</div>
                    <div className="font-mono text-[12px] text-muted-foreground">
                      {[c.phone ? fmtPhone(c.phone) : "no phone", fmtOrder(c.order_id)].filter(Boolean).join(" · ")}
                    </div>
                  </td>
                );
                const campaign = campaignId ? null : <td className="px-4 py-3 text-[12.5px] text-muted-foreground">{c.campaign_name || "Single call"}</td>;

                if (history) {
                  const result = outcomeOf(c.last_outcome ?? c.status);
                  return (
                    <tr key={c.id} className="align-top hover:bg-muted/40">
                      {customer}
                      {campaign}
                      <td className="max-w-80 min-w-56 px-4 py-3">
                        {c.was && <Badge tone={outcomeOf(c.was)?.tone}>{outcomeOf(c.was)?.label ?? c.was}</Badge>}
                        <div className="mt-1 text-[12px] leading-snug text-muted-foreground">{c.was ? (WHY[c.was] ?? c.was.replace(/_/g, " ")) : ""}</div>
                      </td>
                      <td className="px-4 py-3 text-right tabular">{c.attempts || 0}</td>
                      <td className="px-4 py-3 text-[12.5px] whitespace-nowrap text-muted-foreground">{fmtWhen(c.last_call_at) || "—"}</td>
                      <td className="px-4 py-3">
                        <Badge tone={st.tone} dot>
                          {st.label}
                        </Badge>
                        {c.state === "done" && result && (
                          <div className="mt-1">
                            <Badge tone={result.tone}>{result.label}</Badge>
                          </div>
                        )}
                        {c.state_note && <div className="mt-1 text-[12px] text-muted-foreground">{c.state_note}</div>}
                      </td>
                      <td className="px-4 py-2">
                        <div className="flex justify-end">
                          {c.last_call_id != null && (
                            <Button size="sm" variant="ghost" onClick={() => setOpenCall(c.last_call_id)}>
                              <MessageSquare />
                              View call
                            </Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                }

                const why = outcomeOf(c.status);
                const typed = times[c.id];
                return (
                  <tr key={c.id} className="align-top hover:bg-muted/40">
                    {customer}
                    {campaign}
                    <td className="max-w-72 min-w-52 px-4 py-3">
                      {why && <Badge tone={why.tone}>{why.label}</Badge>}
                      <div className="mt-1 text-[12px] leading-snug text-muted-foreground">{reason(c)}</div>
                    </td>
                    <td className="px-4 py-3 text-right whitespace-nowrap tabular">
                      {c.attempts || 0}
                      {c.status !== "call_later" && c.max_attempts ? <span className="text-muted-foreground"> of {c.max_attempts}</span> : null}
                    </td>
                    <td className="px-4 py-3 text-[12.5px] whitespace-nowrap text-muted-foreground">{fmtWhen(c.last_call_at) || "—"}</td>
                    <td className="max-w-60 min-w-44 px-4 py-3">
                      <Badge tone={st.tone} dot>
                        {st.label}
                      </Badge>
                      {c.state === "scheduled" && c.next_try_at && (
                        <div className="mt-1 text-[12.5px] font-medium whitespace-nowrap">
                          {fmtWhen(c.next_try_at)} <span className="font-normal text-muted-foreground">· {fromNow(c.next_try_at, now)}</span>
                        </div>
                      )}
                      <div className="mt-0.5 text-[12px] leading-snug text-muted-foreground">{c.state_note}</div>
                    </td>
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
                        {c.state === "scheduled" && (
                          <Button size="sm" variant="ghost" onClick={() => schedule(c.id, null)} title="Call as soon as possible">
                            ASAP
                          </Button>
                        )}
                      </div>
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
          {data && shown.length === 0 && (
            <p className="px-4 py-10 text-center text-muted-foreground">
              {show === "done" ? "No call-back was rung again in the last 30 days." : "Nobody is waiting for a call back."}
            </p>
          )}
          {!data && <p className="px-4 py-10 text-center text-muted-foreground">{error ? `Could not load the list: ${error}` : "Loading…"}</p>}
        </div>
        <Pager page={paged.page} pages={paged.pages} onPage={paged.setPage} total={paged.total} size={paged.size} />
      </Card>
      {!history && !campaignId && rows.length > 0 && (
        <p className="text-[12.5px] text-muted-foreground">
          {data?.running
            ? "A campaign is running: its own contacts here are rung when their time comes."
            : "Start their campaign (Campaigns) to ring these when their time comes, or press Call now on one."}
        </p>
      )}

      {openCall != null && <CallPanel key={openCall} callId={openCall} onClose={() => setOpenCall((c) => (c === openCall ? null : c))} />}
    </div>
  );
}
