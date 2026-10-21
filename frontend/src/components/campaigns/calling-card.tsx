"use client";

import { useState } from "react";
import { ChevronDown, Play, RotateCcw, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { Select } from "@/components/ui/select";
import { send, type AppConfig, type CampaignRow, type CampaignState, type RunSettings } from "@/lib/api";
import { LANG_LABELS, fitVoice, voicesFor } from "@/lib/voices";
import { cn } from "@/lib/utils";
import { plural } from "./shared";

export type Form = Omit<RunSettings, "campaign_id">;

/** The settings form's starting values: what this campaign last ran with (snapshot.settings). A campaign that never
 *  ran carries the server's bare defaults (a Gemini voice, maybe no agent): those become the first agent and one of
 *  its voices, as on the original page. */
export const formFrom = (s: RunSettings, cfg: AppConfig): Form => {
  const model = cfg.models.includes(s.model) ? s.model : cfg.model;
  return {
    voice: fitVoice(cfg, model, s.voice),
    model,
    language: cfg.languages.includes(s.language) ? s.language : "hinglish",
    max_concurrent: s.max_concurrent,
    max_attempts: s.max_attempts,
    retry_minutes: s.retry_minutes,
    start_hour: s.start_hour,
    end_hour: s.end_hour,
    limit: s.limit,
    budget_rs: s.budget_rs,
    days: s.days || "0123456",
  };
};

type Props = {
  camp: CampaignRow;
  state: CampaignState | null;
  cfg: AppConfig | null;
  form: Form | null;
  setForm: (f: Form) => void;
  onChanged: () => void;
};

/** Step 2: ring everyone on the list. Every call is real and paid, so starting asks first. */
export function CallingCard({ camp, state: s, cfg, form, setForm, onChanged }: Props) {
  const { toast, confirm } = useFeedback();
  const [busy, setBusy] = useState(false);
  const [more, setMore] = useState(false);

  // "running" means THIS campaign is the one calling; another one may be running instead.
  const running = !!s?.running && s.running_campaign_id === s.campaign_id;
  const otherRunning = !!s?.running && !running;
  const calling = s?.by_status.calling || 0;
  const done = s ? Math.max(0, s.total - s.to_call - calling) : 0;
  const retryable = s ? (s.by_status.no_answer || 0) + (s.by_status.busy || 0) + (s.by_status.dropped || 0) : 0;

  let headline = "…";
  let reason = s?.reason || "";
  if (s) {
    if (running && s.in_progress) headline = `Calling… ${plural(s.in_progress, "call")} in progress`;
    else if (running && !s.within_hours)
      headline = s.waiting || `Waiting for calling hours (${s.settings.start_hour}:00 to ${s.settings.end_hour}:00, now ${s.now_ist})`;
    else if (running) headline = "Running · picking the next contact";
    else if (otherRunning) headline = "Another campaign is calling right now · one campaign calls at a time";
    else if (s.past_last_date) headline = `Finished · the last calling day (${s.last_call_date}) has passed`;
    else if (!s.total) headline = "No contacts yet · upload the sheet in step 1";
    else if (s.to_call) headline = `Ready · ${s.to_call} to call`;
    else headline = "List done · nobody left to call";
    if (running && s.calls_made) reason = `${plural(s.calls_made, "call")} placed this run${s.retry_left ? ` · ${s.retry_left} retries queued` : ""}`;
  }

  const set = <K extends keyof Form>(k: K, v: Form[K]) => form && setForm({ ...form, [k]: v });
  const num = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) => set(k, Number(e.target.value) as never);

  const start = async () => {
    if (!s || !form || !cfg) return;
    const n = form.limit > 0 ? Math.min(form.limit, s.to_call) : s.to_call;
    const ok = await confirm({
      title: `Start calling ${plural(n, "contact")}?`,
      body: (
        <>
          {s.dial_only_my_number ? (
            <>
              Test mode: <b>all of them</b> ring <b>your</b> phone {s.my_number}.
            </>
          ) : (
            "They ring the customers' numbers."
          )}
          <br />
          Agent: {cfg.model_labels[form.model] ?? form.model} · {form.voice}
          <br />
          Every call is a real, paid phone call. You can stop at any time; the call in progress finishes first.
        </>
      ),
      confirmLabel: "Start calling",
    });
    if (!ok) return;
    setBusy(true);
    try {
      await send("/api/campaign/start", "POST", { ...form, campaign_id: camp.id, build: cfg.build });
      toast("Campaign started. Live calls show on the Overview.");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await send("/api/campaign/stop", "POST");
      toast("Stopping after the current call.");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
      onChanged();
    }
  };

  const requeue = async () => {
    const ok = await confirm({
      title: "Re-queue no answer / busy?",
      body: "Every no-answer, busy and dropped contact in this campaign goes back on the list with a fresh count of tries.",
      confirmLabel: "Re-queue",
    });
    if (!ok) return;
    try {
      const j = await send<{ requeued: number }>("/api/contacts/requeue", "POST", { statuses: ["no_answer", "busy", "dropped"], campaign_id: camp.id });
      toast(`${plural(j.requeued, "contact")} re-queued.`);
    } catch (e) {
      toast((e as Error).message, true);
    }
    onChanged();
  };

  const toggleAuto = async (on: boolean) => {
    try {
      await send(`/api/campaigns/${camp.id}/auto`, "POST", { auto: on });
      toast(on ? "Marked Automatic: called when Automatic calling is on." : "No longer Automatic.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    onChanged();
  };

  const cancelSchedule = async () => {
    try {
      await send(`/api/campaigns/${camp.id}/schedule`, "POST", { at: null });
      toast("Scheduled start cancelled.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    onChanged();
  };

  const voices = cfg && form ? voicesFor(cfg, form.model) : [];
  const off = running || !form;
  const startsAt = camp.start_at
    ? new Date(camp.start_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false })
    : "";

  return (
    <div className="space-y-4">
      <div>
        <div className="text-[16px] font-medium">{headline}</div>
        {reason && <div className="mt-0.5 text-[13px] text-muted-foreground">{reason}</div>}
      </div>

      {startsAt && !running && (
        <div className="flex flex-wrap items-center gap-3 rounded-md bg-info-soft px-3 py-2 text-[13px] text-info">
          <span>
            Scheduled: starts calling by itself at <b className="font-medium">{startsAt}</b> (Indian time), with the settings below as saved.
          </span>
          <Button size="sm" variant="outline" className="ml-auto" onClick={cancelSchedule}>
            Cancel schedule
          </Button>
        </div>
      )}
      {!startsAt && camp.start_note && <p className="text-[12.5px] text-muted-foreground">Last scheduled start: {camp.start_note}</p>}

      <div>
        <div
          className="flex h-2 overflow-hidden rounded-full bg-muted"
          title={s ? `${done} done · ${calling} in progress · ${s.to_call} to call (of ${s.total})` : ""}
        >
          <div className="h-full bg-brand transition-[width]" style={{ width: s?.total ? `${(100 * done) / s.total}%` : 0 }} />
          <div className="h-full bg-info transition-[width]" style={{ width: s?.total ? `${(100 * calling) / s.total}%` : 0 }} />
        </div>
        <div className="mt-1.5 text-[12.5px] text-muted-foreground">
          {s?.total
            ? `${done} of ${s.total} done${s.retry_left ? ` · ${s.retry_left} waiting for a retry` : ""}${calling ? ` · ${calling} on a call now` : ""}`
            : "Nothing on the list."}
        </div>
      </div>

      {s?.dial_only_my_number && (
        <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
          Test mode: DIAL_ONLY_MY_NUMBER is on, so EVERY call in the campaign rings your own phone ({s.my_number}) instead of the customers. Use “Stop after” to
          keep test runs short.
        </p>
      )}

      {form && cfg && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <Field label="Agent">
            <Select
              tall
              value={form.model}
              disabled={off}
              onChange={(e) => setForm({ ...form, model: e.target.value, voice: fitVoice(cfg, e.target.value, form.voice) })}
            >
              {cfg.models.map((m) => (
                <option key={m} value={m}>
                  {cfg.model_labels[m] ?? m}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Voice">
            <Select tall value={form.voice} disabled={off} onChange={(e) => set("voice", e.target.value)}>
              {voices.map(([n, g]) => (
                <option key={n} value={n}>
                  {n} ({g})
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Language">
            <Select tall value={form.language} disabled={off} onChange={(e) => set("language", e.target.value)}>
              {cfg.languages.map((l) => (
                <option key={l} value={l}>
                  {LANG_LABELS[l] ?? l}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Calling hours (IST)">
            <div className="flex items-center gap-1.5">
              <input
                className={inputClass}
                type="number"
                min={0}
                max={23}
                value={form.start_hour}
                disabled={off}
                onChange={num("start_hour")}
                aria-label="From hour"
              />
              <span className="text-muted-foreground">to</span>
              <input
                className={inputClass}
                type="number"
                min={1}
                max={24}
                value={form.end_hour}
                disabled={off}
                onChange={num("end_hour")}
                aria-label="To hour"
              />
            </div>
          </Field>
          <Field label="Stop after (calls)" hint="0 = the whole list">
            <input className={inputClass} type="number" min={0} value={form.limit} disabled={off} onChange={num("limit")} />
          </Field>
        </div>
      )}

      {form && (
        <div>
          <button
            type="button"
            onClick={() => setMore(!more)}
            className="flex items-center gap-1 text-[13px] font-medium text-muted-foreground hover:text-foreground"
          >
            <ChevronDown className={cn("size-4 transition-transform", more && "rotate-180")} />
            More settings: retries, calls at once, daily budget
          </button>
          {more && (
            <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Field label="Tries per contact" hint="Before giving up on no answer or busy">
                <input className={inputClass} type="number" min={1} max={6} value={form.max_attempts} disabled={off} onChange={num("max_attempts")} />
              </Field>
              <Field label="Retry after (min)">
                <input className={inputClass} type="number" min={1} max={1440} value={form.retry_minutes} disabled={off} onChange={num("retry_minutes")} />
              </Field>
              <Field label="Calls at once" hint={`Up to ${s?.max_concurrent_allowed || 10}`}>
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={s?.max_concurrent_allowed || 10}
                  value={form.max_concurrent}
                  disabled={off}
                  onChange={num("max_concurrent")}
                />
              </Field>
              <Field label="Budget today (₹)" hint="Pauses at this estimated cost · 0 = no cap">
                <input className={inputClass} type="number" min={0} value={form.budget_rs} disabled={off} onChange={num("budget_rs")} />
              </Field>
            </div>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {running ? (
          <Button variant="outline" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={stop} disabled={busy}>
            <Square />
            Stop after the current call
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={start}
            disabled={busy || !s?.to_call || otherRunning || !!s?.past_last_date || !form || !cfg}
            title="Places real, paid phone calls"
          >
            <Play />
            Start calling
          </Button>
        )}
        <Button
          variant="outline"
          onClick={requeue}
          disabled={running || !retryable}
          title="Put no-answer and busy contacts back on the list with a fresh count of tries"
        >
          <RotateCcw />
          Re-queue no answer / busy
        </Button>
      </div>

      <label className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 text-[13px]">
        <input type="checkbox" className="mt-0.5 size-4 accent-[var(--brand)]" checked={!!camp.auto} onChange={(e) => toggleAuto(e.target.checked)} />
        <span>
          <b className="font-medium">Automatic</b>
          <span className="text-muted-foreground">
            : while Automatic calling is on (Store orders page), ring this list&apos;s contacts as they arrive, without pressing Start.
          </span>
        </span>
      </label>
    </div>
  );
}
