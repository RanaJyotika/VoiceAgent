"use client";

import { useState } from "react";
import { Check, Power, PowerOff, Save } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, Field, inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { Select } from "@/components/ui/select";
import { usePoll } from "@/hooks/use-poll";
import { send, type AppConfig, type AutoStatus, type RunSettings, type SkipView, type StoreSettings, type StoreStatus } from "@/lib/api";
import { fmtInt } from "@/lib/format";
import { cn } from "@/lib/utils";
import { LANG_LABELS, fitVoice, voicesFor } from "@/lib/voices";
import { ago, quotaText, when } from "./shared";

type Props = {
  open: boolean;
  onClose: () => void;
  auto: AutoStatus | null;
  store: StoreStatus | null;
  cfg: AppConfig | null;
  onChanged: () => void;
};

type AutoForm = Pick<
  RunSettings,
  "model" | "voice" | "language" | "start_hour" | "end_hour" | "budget_rs" | "max_attempts" | "retry_minutes" | "max_concurrent"
>;

function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="rounded-lg border">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
        <h3 className="text-[15px] font-medium">{title}</h3>
        {aside}
      </header>
      <div className="space-y-4 p-4">{children}</div>
    </section>
  );
}

const num = (v: string) => (v === "" ? 0 : Number(v));

/** Settings of the Store orders page: which orders get a call, and automatic calling. */
export function StoreSettingsDialog({ open, onClose, auto, store, cfg, onChanged }: Props) {
  const { toast, confirm } = useFeedback();
  const skip = usePoll<SkipView>(open ? "/api/store/skip" : null, 30_000);
  const [orders, setOrders] = useState<StoreSettings | null>(null);
  const [form, setForm] = useState<AutoForm | null>(null);
  const [saving, setSaving] = useState(false);
  const [busySkip, setBusySkip] = useState("");

  // Filled once from what the server has saved.
  if (store && !orders) setOrders(store.settings);
  if (auto && cfg && !form) {
    const s = auto.settings;
    const model = cfg.models.includes(s.model) ? s.model : cfg.model;
    setForm({
      model,
      voice: fitVoice(cfg, model, s.voice || cfg.voice),
      language: s.language || "hinglish",
      start_hour: s.start_hour,
      end_hour: s.end_hour,
      budget_rs: s.budget_rs,
      max_attempts: s.max_attempts,
      retry_minutes: s.retry_minutes,
      max_concurrent: s.max_concurrent,
    });
  }

  const saveOrders = async () => {
    if (!orders) return;
    setSaving(true);
    try {
      await send("/api/shopify/settings", "PUT", orders);
      toast("Saved.");
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };

  const toggleSkip = async (key: string) => {
    if (!skip.data) return;
    const on = skip.data.rules.filter((r) => (r.key === key ? !r.on : r.on)).map((r) => r.key);
    setBusySkip(key);
    try {
      const v = await send<SkipView>("/api/store/skip", "PUT", { on });
      const c = v.changed;
      toast(c && (c.skipped || c.back) ? `Saved. ${c.skipped} order(s) taken off the queue, ${c.back} put back.` : "Saved.");
      skip.reload();
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusySkip("");
    }
  };

  const switchOn = async () => {
    if (!form || !auto || !cfg) return;
    const n = auto.campaigns.reduce((t, c) => t + c.to_call, 0);
    const ok = await confirm({
      title: "Switch on automatic calling?",
      body: (
        <>
          Rings {auto.dial_only_my_number ? <b>your own phone {auto.my_number} (test mode)</b> : "the customers' numbers"}, with{" "}
          {cfg.model_labels[form.model] ?? form.model} · {form.voice}.
          <br />
          {plural(n, "contact")} waiting now, and every new one is called as it arrives, between {form.start_hour}:00 and {form.end_hour}:00, without anyone
          pressing Start.
          <br />
          Every call is a real, paid phone call. It stays on after a server restart until you switch it off.
        </>
      ),
      confirmLabel: "Switch on",
    });
    if (!ok) return;
    try {
      await send("/api/auto/start", "POST", { ...form, build: cfg.build });
      toast("Automatic calling is on.");
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const switchOff = async () => {
    try {
      await send("/api/auto/stop", "POST");
      toast("Automatic calling is off. A call in progress will finish.");
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const setO = <K extends keyof StoreSettings>(k: K, v: StoreSettings[K]) => orders && setOrders({ ...orders, [k]: v });
  const setF = <K extends keyof AutoForm>(k: K, v: AutoForm[K]) => form && setForm({ ...form, [k]: v });
  const running = !!auto?.running;
  const asOf = skip.updatedAt;

  let state = "Off";
  if (auto?.running && auto.in_progress) state = `On · ${plural(auto.in_progress, "call")} in progress`;
  else if (auto?.running) state = `On · ${auto.reason || "starting"}`;
  else if (auto?.enabled) state = "On, but not running";
  const warn = [
    auto?.dial_only_my_number && `Test mode: every automatic call rings your own phone (${auto.my_number}).`,
    auto &&
      !auto.running &&
      auto.phone_missing.length > 0 &&
      `Phone line not ready (missing ${auto.phone_missing.join(", ")}). Start the server with run_phone.bat.`,
  ].filter(Boolean);

  return (
    <Dialog open={open} onClose={onClose} title="Store order settings" description="Which orders get a call, and automatic calling." className="max-w-[880px]">
      <div className="space-y-4">
        <Section
          title="Which orders get a call"
          aside={
            store && (
              <>
                {!store.enabled ? (
                  <Badge tone="warn" dot>
                    Not connected
                  </Badge>
                ) : store.error ? (
                  <Badge tone="bad" dot>
                    Cannot read
                  </Badge>
                ) : (
                  <Badge tone="good" dot>
                    Reading
                  </Badge>
                )}
                <span className="text-[12.5px] text-muted-foreground">
                  {store.enabled
                    ? `checked ${ago(store.last_check, asOf || new Date(store.last_check || 0).getTime())}`
                    : "Add MONGODB_URI to .env and restart."}
                </span>
              </>
            )
          }
        >
          {store?.error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[12.5px] text-bad">The database could not be read: {store.error}</p>}
          {orders && store && (
            <div className="grid gap-4 sm:grid-cols-3">
              <Field label="Orders">
                <Select tall value={orders.orders} onChange={(e) => setO("orders", e.target.value as StoreSettings["orders"])}>
                  <option value="cod">Cash on delivery only</option>
                  <option value="all">All orders</option>
                  <option value="off">None</option>
                </Select>
              </Field>
              <Field label="Wait after the order (min)" hint="Time for the customer to cancel or edit it first.">
                <input
                  className={inputClass}
                  type="number"
                  min={0}
                  max={1440}
                  value={orders.order_wait_minutes}
                  onChange={(e) => setO("order_wait_minutes", num(e.target.value))}
                />
              </Field>
              <Field label="Stop trying after (hours)" hint="Not called by then: off the list (likely shipped).">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={168}
                  value={orders.order_max_hours}
                  onChange={(e) => setO("order_max_hours", num(e.target.value))}
                />
              </Field>
              <Field label="Which orders" hint="The days the orders were placed on (Indian dates).">
                <Select tall value={orders.order_days || "all"} onChange={(e) => setO("order_days", e.target.value)}>
                  {Object.entries(store.order_days).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Calls a day" hint="0 = no limit. Calls you place yourself and retries do not count.">
                <input
                  className={inputClass}
                  type="number"
                  min={0}
                  max={5000}
                  value={orders.order_daily_limit}
                  onChange={(e) => setO("order_daily_limit", num(e.target.value))}
                />
              </Field>
              <Field label="Which ones first">
                <Select tall value={orders.order_pick || "newest"} onChange={(e) => setO("order_pick", e.target.value)}>
                  <option value="newest">Newest first</option>
                  <option value="oldest">Oldest first</option>
                  <option value="random">Random</option>
                </Select>
              </Field>
            </div>
          )}

          <div>
            <div className="mb-2 text-[13.5px] font-medium">
              Don&apos;t call orders that are <span className="font-normal text-muted-foreground">· tap to switch, any number at once; takes effect now</span>
            </div>
            <div className="flex flex-wrap gap-1.5" role="group" aria-label="Don't call orders that are">
              {(skip.data?.rules ?? []).map((r) => (
                <button
                  key={r.key}
                  type="button"
                  aria-pressed={r.on}
                  disabled={busySkip === r.key}
                  onClick={() => toggleSkip(r.key)}
                  className={cn(
                    "inline-flex h-8 items-center gap-1.5 rounded-full border px-3 text-[13px] font-medium transition-colors disabled:opacity-60",
                    r.on ? "border-primary bg-primary text-primary-foreground" : "border-input bg-card text-foreground/85 hover:bg-muted",
                  )}
                >
                  {r.on && <Check className="size-3.5" />}
                  {r.label}
                </button>
              ))}
            </div>
            {skip.data && (
              <p className="mt-2 text-[12px] text-muted-foreground">
                {skip.data.rules.some((r) => r.on)
                  ? `${fmtInt(skip.data.not_calling)} waiting order${skip.data.not_calling === 1 ? "" : "s"} not being called because of these; they show as "No call" with the reason.`
                  : "All switches off: every order on the list gets a call."}
              </p>
            )}
          </div>

          {store && (
            <p className="text-[12.5px] text-muted-foreground">
              {[store.since ? `New orders are added from ${when(store.since)}.` : "", `Automatic calling rings ${quotaText(store.quota)}.`]
                .filter(Boolean)
                .join(" ")}
            </p>
          )}
          <div className="flex justify-end">
            <Button variant="primary" onClick={saveOrders} disabled={saving || !orders}>
              <Save />
              {saving ? "Saving…" : "Save"}
            </Button>
          </div>
        </Section>

        <Section
          title="Automatic calling"
          aside={
            auto && (
              <Badge tone={running ? "good" : "neutral"} dot>
                {state}
              </Badge>
            )
          }
        >
          <p className="-mt-1 text-[12.5px] text-muted-foreground">
            When on, orders under &quot;Left to call&quot; (and campaigns marked Automatic) are rung by themselves, inside the calling hours.
            {running && auto?.calls_made ? ` ${plural(auto.calls_made, "call")} since switched on.` : ""}
          </p>
          {warn.length > 0 && <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">{warn.join(" ")}</p>}
          {auto && auto.campaigns.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {auto.campaigns.map((c) => (
                <a
                  key={c.id}
                  href={`/campaigns/view/?id=${c.id}`}
                  title={c.call_type_name}
                  className="inline-flex h-8 items-center gap-2 rounded-full border border-input bg-card px-3 text-[13px] hover:bg-muted"
                >
                  {c.name}
                  <span className="rounded-full bg-muted px-1.5 text-[12px] font-semibold tabular">{fmtInt(c.to_call)}</span>
                </a>
              ))}
            </div>
          )}
          {form && cfg && (
            <fieldset disabled={running} className="grid gap-4 disabled:opacity-70 sm:grid-cols-3">
              <Field label="Agent">
                <Select
                  tall
                  value={form.model}
                  onChange={(e) => form && setForm({ ...form, model: e.target.value, voice: fitVoice(cfg, e.target.value, form.voice) })}
                >
                  {cfg.models.map((m) => (
                    <option key={m} value={m}>
                      {cfg.model_labels[m] ?? m}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Voice">
                <Select tall value={form.voice} onChange={(e) => setF("voice", e.target.value)}>
                  {voicesFor(cfg, form.model).map(([v, g]) => (
                    <option key={v} value={v}>
                      {v} · {g}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Language" hint="A row's own language, if it has one, wins.">
                <Select tall value={form.language} onChange={(e) => setF("language", e.target.value)}>
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
                    onChange={(e) => setF("start_hour", num(e.target.value))}
                  />
                  <span className="text-muted-foreground">to</span>
                  <input className={inputClass} type="number" min={1} max={24} value={form.end_hour} onChange={(e) => setF("end_hour", num(e.target.value))} />
                </div>
              </Field>
              <Field label="Budget per day (₹)" hint="Waits for the next day once reached. 0 = no cap.">
                <input className={inputClass} type="number" min={0} value={form.budget_rs} onChange={(e) => setF("budget_rs", num(e.target.value))} />
              </Field>
              <Field label="Tries per contact">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={6}
                  value={form.max_attempts}
                  onChange={(e) => setF("max_attempts", num(e.target.value))}
                />
              </Field>
              <Field label="Retry after (min)">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={1440}
                  value={form.retry_minutes}
                  onChange={(e) => setF("retry_minutes", num(e.target.value))}
                />
              </Field>
              <Field label="Calls at once">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={10}
                  value={form.max_concurrent}
                  onChange={(e) => setF("max_concurrent", num(e.target.value))}
                />
              </Field>
            </fieldset>
          )}
          <div className="flex flex-wrap items-center justify-end gap-3">
            {running && auto && (
              <span className="mr-auto text-[12.5px] text-muted-foreground">
                Calling hours {auto.settings.start_hour}:00 to {auto.settings.end_hour}:00 IST (now {auto.now_ist})
                {auto.settings.budget_rs ? ` · ₹${auto.settings.budget_rs} a day` : ""}. Switch off to change the settings.
              </span>
            )}
            {running ? (
              <Button variant="outline" className="border-bad/40 text-bad hover:bg-bad-soft" onClick={switchOff}>
                <PowerOff />
                Switch off
              </Button>
            ) : (
              <Button variant="primary" onClick={switchOn} disabled={!form}>
                <Power />
                Switch on
              </Button>
            )}
          </div>
        </Section>
      </div>
    </Dialog>
  );
}

const plural = (n: number, word: string) => `${fmtInt(n)} ${word}${n === 1 ? "" : "s"}`;
