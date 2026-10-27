"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, ArrowLeft, Check, FileSpreadsheet, Keyboard, Pencil, ShoppingBag, Trash2, X, type LucideIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button, buttonClass } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { Select } from "@/components/ui/select";
import { usePoll } from "@/hooks/use-poll";
import { useCallTypes, useConfig } from "@/hooks/use-static";
import { getJson, send, type CampaignRow, type ContactRow, type RunSettings } from "@/lib/api";
import { fmtOrder, fmtPhone, fmtRs } from "@/lib/format";
import { MEANINGS, outcomeOf, tagOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";
import { LANG_LABELS, fitVoice, remembered, voicesFor } from "@/lib/voices";
import { formFrom, type Form } from "./calling-card";
import { ContactsCard } from "./contacts-card";
import { OffersEditor, offersJson, parseOffers } from "./offers-editor";
import { WhatsappCoupon } from "./whatsapp-coupon";
import { FINAL, campaignHref, plural } from "./shared";
import { StoreFetch } from "./store-fetch";

const STEPS = ["Details", "Agent", "Audience", "Facts & offers", "Calling rules", "Schedule", "Review"] as const;
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** What the server starts a campaign with when nothing was chosen (campaign.Settings). */
const DEFAULTS: RunSettings = {
  campaign_id: 0,
  voice: "",
  model: "",
  language: "hinglish",
  max_concurrent: 1,
  max_attempts: 3,
  retry_minutes: 30,
  start_hour: 10,
  end_hour: 19,
  limit: 0,
  budget_rs: 0,
  days: "0123456",
};

type SavedCampaign = CampaignRow & { settings: Partial<RunSettings> | null };
type Audience = "upload" | "store" | "manual";
type StopWhen = "exhausted" | "date" | "calls";

const hh = (h: number) => `${String(h).padStart(2, "0")}:00`;

/** "012345" -> "Mon – Sat"; "0246" -> "Mon, Wed, Fri, Sun". */
function daysText(days: string) {
  const on = [...days].map(Number).sort();
  if (on.length === 7) return "Every day";
  const run = on.every((d, i) => i === 0 || d === on[i - 1] + 1);
  return run && on.length > 2 ? `${DAY_NAMES[on[0]]} – ${DAY_NAMES[on[on.length - 1]]}` : on.map((d) => DAY_NAMES[d]).join(", ");
}

/** When the first call can go under the window and days, in Indian time: "" = right now, else e.g. "Mon 26 Oct at 13:00". */
function nextCallText(f: Pick<RunSettings, "start_hour" | "end_hour" | "days">) {
  const ist = new Date(Date.now() + 330 * 60_000); // read with the UTC getters
  const dayOf = (d: Date) => String((d.getUTCDay() + 6) % 7); // Mon = 0, as the server counts
  if (f.days.includes(dayOf(ist)) && f.start_hour <= ist.getUTCHours() && ist.getUTCHours() < f.end_hour) return "";
  for (let add = 0; add < 8; add++) {
    const at = new Date(ist);
    at.setUTCDate(at.getUTCDate() + add);
    at.setUTCHours(f.start_hour, 0, 0, 0);
    if (at > ist && f.days.includes(dayOf(at)))
      return `${DAY_NAMES[Number(dayOf(at))]} ${at.getUTCDate()} ${at.toLocaleString("en-IN", { month: "short", timeZone: "UTC" })} at ${hh(f.start_hour)}`;
  }
  return "never";
}

/** One campaign detail as saved: offers as their JSON list; the WhatsApp coupon only while its offer is still there. */
function factValue(key: string, facts: Record<string, string>) {
  if (key === "offers") return offersJson(parseOffers(facts.offers));
  const value = (facts[key] ?? "").trim();
  if (key !== "whatsapp_coupon" || !value) return value;
  return parseOffers(facts.offers).some((o) => o.code.trim().toUpperCase() === value.toUpperCase() && (o.title.trim() || o.details.trim()))
    ? value.toUpperCase()
    : "";
}

/** "2026-10-22T10:00" -> "22 Oct, 10:00" */
const whenText = (local: string) =>
  local ? new Date(local).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "";

/** A result tag with what it means on hover (or keyboard focus). */
function Tag({ name, meaning, tone }: { name: string; meaning: string; tone?: Parameters<typeof Badge>[0]["tone"] }) {
  return (
    <span className="group relative inline-flex" tabIndex={0}>
      <Badge tone={tone} className="cursor-help font-mono">
        {name}
      </Badge>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1.5 w-56 -translate-x-1/2 rounded-md bg-primary px-2.5 py-1.5 text-[12px] leading-snug text-primary-foreground opacity-0 shadow-lg transition-opacity group-hover:opacity-100 group-focus:opacity-100"
      >
        {meaning}
      </span>
    </span>
  );
}

/** New campaign as a wizard (?type= picks the template and locks it), or ?edit=<id>[&step=n] to go on with one.
 *  Continue on Details creates the campaign as a draft (the audience needs it to exist); each later Continue saves.
 *  The last step starts calling now, sets the scheduled start, or only saves. */
export function CampaignForm() {
  const router = useRouter();
  const params = useSearchParams();
  const types = useCallTypes();
  const cfg = useConfig();
  const { toast, confirm } = useFeedback();
  const template = params.get("type") ?? "";

  const [id, setId] = useState(Number(params.get("edit") || 0));
  const [step, setStep] = useState(Math.min(STEPS.length, Math.max(1, Number(params.get("step") || 1))));
  const [loaded, setLoaded] = useState<SavedCampaign | null>(null);
  const [name, setName] = useState("");
  const [type, setType] = useState(template);
  const [last, setLast] = useState("");
  const [facts, setFacts] = useState<Record<string, string>>({});
  const [auto, setAuto] = useState(false);
  const [form, setForm] = useState<Form | null>(null);
  const [audience, setAudience] = useState<Audience>("upload");
  const [startMode, setStartMode] = useState<"now" | "later">("now");
  const [startAt, setStartAt] = useState("");
  const [stopWhen, setStopWhen] = useState<StopWhen>("exhausted");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // An existing campaign: everything it has, settings and schedule included.
  useEffect(() => {
    if (!id || loaded?.id === id) return;
    getJson<SavedCampaign>(`/api/campaigns/${id}`)
      .then((c) => {
        setLoaded(c);
        setName(c.name);
        setType(c.call_type);
        setLast(c.last_call_date || "");
        setFacts(c.facts || {});
        setAuto(!!c.auto);
        if (c.start_at) {
          setStartMode("later");
          setStartAt(c.start_at.slice(0, 16)); // stored in Indian time: "2026-10-22T10:00:00+05:30"
        }
        setStopWhen(c.last_call_date ? "date" : c.settings?.limit ? "calls" : "exhausted");
      })
      .catch((e) => setError(`Could not load the campaign: ${e.message}`));
  }, [id, loaded?.id]);

  const t = types?.find((x) => x.key === type);

  // A new campaign from a template is named after it and today: "Order confirmation — 21 Oct".
  if (!id && t && !name) setName(`${t.name} — ${new Date().toLocaleDateString("en-IN", { day: "numeric", month: "short" })}`);

  // The settings start from what the campaign has saved, else the defaults; agent and voice fitted to what exists.
  if (cfg && !form && (!id || loaded)) {
    const saved = { ...DEFAULTS, voice: remembered("voice") || cfg.voice, ...(loaded?.settings ?? {}) };
    setForm(formFrom(saved, cfg));
  }

  const contacts = usePoll<ContactRow[]>(id ? `/api/contacts?campaign_id=${id}` : null, step === 3 || step === 7 ? 5_000 : 30_000);
  const missingFacts = (t?.facts ?? []).filter((f) => f.required && !(facts[f.key] ?? "").trim());
  const typeLocked = !!id || !!template;
  const fromStore = type === "order_confirmation"; // store orders fill order confirmations

  const go = (n: number, campaignId = id) => {
    setStep(n);
    setError("");
    if (campaignId) router.replace(`/campaigns/new/?edit=${campaignId}&step=${n}`, { scroll: false });
    window.scrollTo({ top: 0 });
  };

  /** Save everything typed so far. draft: required campaign details may still be empty. */
  const save = async (draft = true) => {
    if (!type) throw new Error("Pick the type of campaign.");
    if (!name.trim()) throw new Error("Give the campaign a name.");
    if (stopWhen === "date" && !last) throw new Error("Pick the last calling day (Schedule).");
    const body = {
      name: name.trim(),
      call_type: type,
      last_call_date: stopWhen === "date" ? last : "",
      facts: Object.fromEntries((t?.facts ?? []).map((f) => [f.key, factValue(f.key, facts)])),
      settings: form ? { ...form, limit: stopWhen === "calls" ? form.limit : 0 } : undefined,
      draft,
    };
    const c = id ? await send<SavedCampaign>(`/api/campaigns/${id}`, "PUT", body) : await send<SavedCampaign>("/api/campaigns", "POST", body);
    if (!!loaded?.auto !== auto) await send(`/api/campaigns/${c.id}/auto`, "POST", { auto });
    setLoaded({ ...c, auto: auto ? 1 : 0 });
    if (!id) {
      setId(c.id);
      toast("Saved as a draft. Next: choose the agent.");
    }
    return c.id;
  };

  const next = async () => {
    setSaving(true);
    setError("");
    try {
      const cid = step === 3 ? id : await save(); // the audience is saved as it is added
      go(step + 1, cid);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  /** The last step: save, then start calling now (after a yes) or set the scheduled start. start=false: only save. */
  const finish = async (start: boolean) => {
    setError("");
    if (start && startMode === "later" && !startAt) return setError("Pick when it starts (Schedule).");
    const n = contacts.data?.length ?? 0;
    if (start && startMode === "now" && form) {
      // Calls only go on the chosen days, inside the window: say so when that is not now (owner, 21 Oct 2026).
      const next = nextCallText(form);
      const ok = await confirm({
        title: next ? `No call goes right now` : `Start calling ${plural(n, "contact")} now?`,
        body: next ? (
          <>
            This campaign calls only {daysText(form.days)}, {hh(form.start_hour)}–{hh(form.end_hour)} IST, and now is outside that. If you start it, the first
            call goes <b className="font-medium text-foreground">{next}</b>.
            <br />
            To call now, press Cancel: you go back to Schedule, where you can add today and a window that covers this hour.
          </>
        ) : (
          "Every call is a real, paid phone call, placed inside the calling window. You can stop at any time from the campaign page."
        ),
        confirmLabel: next ? "Start and wait" : "Start calling",
      });
      if (!ok) {
        if (next) go(STEPS.indexOf("Schedule") + 1);
        return;
      }
    }
    if (start && startMode === "later" && form) {
      const at = new Date(`${startAt}:00Z`); // the picked time is Indian time; read it with the UTC getters
      const inside = form.days.includes(String((at.getUTCDay() + 6) % 7)) && form.start_hour <= at.getUTCHours() && at.getUTCHours() < form.end_hour;
      if (!inside) {
        const ok = await confirm({
          title: "The start time is outside the calling window",
          body: `At ${whenText(startAt)} nothing is called: this campaign calls only ${daysText(form.days)}, ${hh(form.start_hour)}–${hh(form.end_hour)} IST. It starts then but waits for the next calling time. Press Cancel to change the time, days or window.`,
          confirmLabel: "Schedule anyway",
        });
        if (!ok) return;
      }
    }
    setSaving(true);
    try {
      const cid = await save(false);
      if (!start) {
        toast("Saved. Nothing is called until you start it.");
      } else if (startMode === "later") {
        await send(`/api/campaigns/${cid}/schedule`, "POST", { at: startAt });
        toast(`Scheduled: calling starts ${whenText(startAt)}.`);
      } else {
        if (loaded?.start_at) await send(`/api/campaigns/${cid}/schedule`, "POST", { at: null });
        await send("/api/campaign/start", "POST", { ...form, limit: stopWhen === "calls" ? form?.limit : 0, campaign_id: cid, build: cfg?.build });
        toast("Calling started. Live calls show on the Overview.");
      }
      router.push(campaignHref(cid));
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  };

  const removeOne = async (contactId: number) => {
    try {
      await send(`/api/campaigns/${id}/contacts/${contactId}`, "DELETE");
    } catch (e) {
      toast((e as Error).message, true);
    }
    contacts.reload();
  };

  const clearList = async () => {
    const ok = await confirm({
      title: "Clear the call list?",
      body: "Every customer on this campaign's list is taken off. Call history keeps its records.",
      confirmLabel: "Clear list",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`/api/campaigns/${id}/contacts`, "DELETE");
      toast("List cleared.");
    } catch (e) {
      toast((e as Error).message, true);
    }
    contacts.reload();
  };

  const set = <K extends keyof Form>(k: K, v: Form[K]) => form && setForm({ ...form, [k]: v });
  const num = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => set(k, Number(e.target.value) as never);
  const toggleDay = (d: number) => {
    if (!form) return;
    const on = form.days.includes(String(d)) ? form.days.replace(String(d), "") : form.days + d;
    if (on) set("days", [...on].sort().join(""));
  };
  const back = id ? campaignHref(id) : "/campaigns/";
  const voices = cfg && form ? voicesFor(cfg, form.model) : [];
  const success = (t?.positive ?? []).map((k) => outcomeOf(k)?.label ?? k);
  const tags = (t?.outcomes ?? []).filter((o) => FINAL.includes(o));
  const required = (t?.columns ?? []).filter((c) => c.required).map((c) => c.label);
  const optional = (t?.columns ?? []).filter((c) => !c.required).map((c) => c.label);
  const tries = (n: number) => `${n} ${n === 1 ? "try" : "tries"}`;

  const audienceOptions: { key: Audience; icon: LucideIcon; title: string; text: string; off?: string }[] = [
    { key: "upload", icon: FileSpreadsheet, title: "Upload the customer sheet", text: "Excel or CSV, one row per customer" },
    {
      key: "store",
      icon: ShoppingBag,
      title: "Fetch from store",
      text: "Orders of the days you pick",
      off: fromStore ? undefined : "Store orders fill order confirmation campaigns",
    },
    { key: "manual", icon: Keyboard, title: "Enter numbers by hand", text: "One customer at a time" },
  ];

  return (
    <div className="pb-10">
      <div className="flex items-center gap-3 border-b px-4 pt-5 pb-4 sm:px-6">
        <Link href={back} className={buttonClass("ghost", "icon")} aria-label="Back">
          <ArrowLeft />
        </Link>
        <div className="min-w-0">
          <h1 className="truncate text-[20px] font-semibold tracking-tight">{id ? name || "Campaign" : "New campaign"}</h1>
          <p className="text-[13px] text-muted-foreground">{id ? "Saved as you go · nothing rings until you start it" : "Set it up step by step"}</p>
        </div>
      </div>

      <div className="grid gap-6 px-4 pt-5 sm:px-6 lg:grid-cols-[220px_minmax(0,1fr)]">
        <nav aria-label="Steps" className="-mx-1 flex gap-1 overflow-x-auto pb-1 lg:mx-0 lg:flex-col lg:overflow-visible">
          {STEPS.map((label, i) => {
            const n = i + 1;
            const on = n === step;
            const done = !!id && n < step;
            return (
              <button
                key={label}
                type="button"
                disabled={n !== 1 && !id}
                onClick={() => go(n)}
                aria-current={on ? "step" : undefined}
                title={n !== 1 && !id ? "Continue from Details first" : undefined}
                className={cn(
                  "flex h-9 shrink-0 items-center gap-3 rounded-md px-2.5 text-left text-[14px] transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                  on ? "bg-muted font-medium text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                )}
              >
                <span
                  className={cn(
                    "grid size-5 shrink-0 place-items-center rounded-full border text-[11px]",
                    on ? "border-foreground text-foreground" : done ? "border-good bg-good text-white" : "border-input",
                  )}
                >
                  {done ? <Check className="size-3" /> : n}
                </span>
                {label}
              </button>
            );
          })}
        </nav>

        <div className="min-w-0 space-y-4">
          <Card>
            <div className="border-b px-4 py-3">
              <h2 className="text-[15px] font-medium">{STEPS[step - 1]}</h2>
            </div>
            <div className="p-4">
              {step === 1 && (
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Campaign name" required>
                    <input className={inputClass} maxLength={120} value={name} onChange={(e) => setName(e.target.value)} />
                  </Field>
                  <Field label="Type" hint={typeLocked ? (id ? "Fixed once the campaign exists." : "Set by the template you picked.") : undefined}>
                    <Select tall value={type} disabled={typeLocked} onChange={(e) => setType(e.target.value)}>
                      {!type && <option value="">Pick a type…</option>}
                      {(types ?? []).map((ct) => (
                        <option key={ct.key} value={ct.key}>
                          {ct.name}
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <div className="sm:col-span-2">
                    <Field label="Goal / success definition" hint="Set by the type: a call counts as a success only with a clear yes from the customer.">
                      <div className="min-h-15 rounded-md border border-input bg-muted/40 px-3 py-2 text-[14px] leading-snug">
                        {t ? (
                          <>
                            {t.description}
                            {success.length > 0 && <span className="text-muted-foreground"> Success: {success.join(", ")}.</span>}
                          </>
                        ) : (
                          <span className="text-muted-foreground">Pick a type to see what its calls do.</span>
                        )}
                      </div>
                    </Field>
                  </div>
                  <div>
                    <div className="mb-1.5 text-[13.5px] font-medium">Outcomes</div>
                    <div className="flex min-h-10 flex-wrap items-center gap-1.5 rounded-md border border-input bg-muted/40 px-3 py-2">
                      {t && <Tag name="voice-call" meaning="Added to every order that got an AI call, whatever its result." />}
                      {tags.map((o) => (
                        <Tag key={o} name={tagOf(o)} meaning={MEANINGS[o] ?? o} tone={outcomeOf(o)?.tone} />
                      ))}
                      {!t && <span className="text-[14px] text-muted-foreground">—</span>}
                    </div>
                    <p className="mt-1 text-[12px] text-muted-foreground">
                      Tags our code puts on the order when a call ends (Shopify). Hover one for what it means.
                    </p>
                  </div>
                  <Field label="Budget cap (₹)" hint="Calling pauses when today's spend on all calls reaches this · 0 = no cap.">
                    <input className={inputClass} type="number" min={0} value={form?.budget_rs ?? 0} onChange={num("budget_rs")} />
                  </Field>
                </div>
              )}

              {step === 2 && form && cfg && (
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Agent" hint="The AI that makes every call.">
                    <Select
                      tall
                      value={form.model}
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
                    <Select tall value={form.voice} onChange={(e) => set("voice", e.target.value)}>
                      {voices.map(([n, g]) => (
                        <option key={n} value={n}>
                          {n} ({g})
                        </option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Language" hint="A sheet's own language column wins for its rows.">
                    <Select tall value={form.language} onChange={(e) => set("language", e.target.value)}>
                      {cfg.languages.map((l) => (
                        <option key={l} value={l}>
                          {LANG_LABELS[l] ?? l}
                        </option>
                      ))}
                    </Select>
                  </Field>
                </div>
              )}

              {/* Kept loaded while other steps show, so what was typed or picked here is still here on coming back. */}
              {loaded && (
                <div className="space-y-4" hidden={step !== 3}>
                  <div className="grid gap-3 sm:grid-cols-3" role="radiogroup" aria-label="Where the contacts come from">
                    {audienceOptions.map((o) => (
                      <button
                        key={o.key}
                        type="button"
                        role="radio"
                        aria-checked={audience === o.key}
                        disabled={!!o.off}
                        title={o.off}
                        onClick={() => setAudience(o.key)}
                        className={cn(
                          "relative rounded-lg border p-3.5 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50",
                          audience === o.key ? "border-brand bg-brand-soft/60 ring-1 ring-brand" : "hover:bg-muted/50",
                        )}
                      >
                        {audience === o.key && <Check className="absolute top-3 right-3 size-4 text-brand" />}
                        <o.icon className="size-4.5 text-muted-foreground" />
                        <div className="mt-2 font-medium">{o.title}</div>
                        <div className="text-[12.5px] text-muted-foreground">{o.off ?? o.text}</div>
                      </button>
                    ))}
                  </div>

                  <div hidden={audience !== "upload"}>
                    <ContactsCard
                      camp={loaded}
                      type={t}
                      count={contacts.data?.length ?? 0}
                      onChanged={contacts.reload}
                      show="upload"
                      uploadNote={
                        <p className="text-[12.5px] leading-relaxed text-subtle-foreground">
                          The sheet must have: {required.join(", ")}.{optional.length > 0 && <> Optional: {optional.join(", ")}.</>}
                        </p>
                      }
                    />
                  </div>
                  {fromStore && (
                    <div hidden={audience !== "store"}>
                      <StoreFetch campaignId={id} onChanged={contacts.reload} />
                    </div>
                  )}
                  <div hidden={audience !== "manual"}>
                    <ContactsCard camp={loaded} type={t} count={contacts.data?.length ?? 0} onChanged={contacts.reload} show="manual" />
                  </div>

                  <div className="rounded-lg border">
                    <div className="flex items-center gap-3 border-b px-3 py-2">
                      <div>
                        <div className="text-[13.5px] font-medium">Call list: {contacts.data ? plural(contacts.data.length, "customer") : "…"}</div>
                        <div className="text-[12px] text-muted-foreground">
                          These are the customers this campaign calls. Take off anyone you do not want called.
                        </div>
                      </div>
                      {!!contacts.data?.length && (
                        <Button variant="outline" size="sm" className="ml-auto border-bad/40 text-bad hover:bg-bad-soft" onClick={clearList}>
                          <Trash2 />
                          Clear list
                        </Button>
                      )}
                    </div>
                    {contacts.data && contacts.data.length > 0 ? (
                      <div className="max-h-90 overflow-auto">
                        <table className="w-full text-left text-[13px]">
                          <tbody className="divide-y">
                            {contacts.data.slice(0, 500).map((c) => {
                              const removable = c.status === "pending" && !c.attempts;
                              return (
                                <tr key={c.id} className="hover:bg-muted/40">
                                  <td className="px-3 py-1.5">{c.customer_name}</td>
                                  <td className="px-3 py-1.5 font-mono text-[12.5px] text-muted-foreground">{fmtPhone(c.phone)}</td>
                                  <td className="px-3 py-1.5 text-muted-foreground">{[fmtOrder(c.order_id), c.product].filter(Boolean).join(" · ")}</td>
                                  <td className="w-10 px-2 py-1 text-right">
                                    {removable ? (
                                      <button
                                        type="button"
                                        onClick={() => removeOne(c.id)}
                                        className="rounded p-1 text-muted-foreground hover:bg-bad-soft hover:text-bad"
                                        aria-label={`Take ${c.customer_name} off the list`}
                                        title="Take off the list"
                                      >
                                        <X className="size-4" />
                                      </button>
                                    ) : (
                                      <span className="text-[12px] text-muted-foreground" title="Already called: stays on the list">
                                        called
                                      </span>
                                    )}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                        {contacts.data.length > 500 && (
                          <p className="border-t px-3 py-2 text-[12px] text-muted-foreground">Showing 500 of {contacts.data.length}; all of them are called.</p>
                        )}
                      </div>
                    ) : (
                      <p className="px-3 py-3 text-[13px] text-muted-foreground">Nobody yet. Add customers with one of the options above.</p>
                    )}
                  </div>
                </div>
              )}

              {step === 4 && t && (
                <div className="space-y-4">
                  {t.facts.length === 0 ? (
                    <p className="text-[13.5px] text-muted-foreground">Nothing to fill in: everything the agent needs comes from each row of the sheet.</p>
                  ) : (
                    <div className="grid gap-4 sm:grid-cols-2">
                      {t.facts.map((f) =>
                        f.key === "offers" ? (
                          <div key={f.key} className="sm:col-span-2">
                            <OffersEditor value={facts.offers} onChange={(json) => setFacts({ ...facts, offers: json })} />
                          </div>
                        ) : f.key === "whatsapp_coupon" ? (
                          <div key={f.key} className="sm:col-span-2">
                            <WhatsappCoupon
                              value={facts.whatsapp_coupon}
                              offers={facts.offers}
                              onChange={(code) => setFacts({ ...facts, whatsapp_coupon: code })}
                            />
                          </div>
                        ) : (
                          <div key={f.key} className={f.long ? "sm:col-span-2" : undefined}>
                            <Field
                              label={f.label.replace(" (optional)", "")}
                              required={f.required}
                              hint={f.key === "answers" ? "Short facts the agent may state. One per line." : undefined}
                            >
                              {f.long ? (
                                <textarea
                                  className={cn(inputClass, "h-auto min-h-21 resize-y py-2 leading-snug")}
                                  placeholder={f.hint}
                                  value={facts[f.key] ?? ""}
                                  onChange={(e) => setFacts({ ...facts, [f.key]: e.target.value })}
                                />
                              ) : (
                                <input
                                  className={cn(inputClass, f.key === "offer_code" && "font-mono uppercase")}
                                  type={f.date ? "date" : "text"}
                                  placeholder={f.hint}
                                  value={facts[f.key] ?? ""}
                                  onChange={(e) => setFacts({ ...facts, [f.key]: e.target.value })}
                                />
                              )}
                            </Field>
                          </div>
                        ),
                      )}
                    </div>
                  )}
                </div>
              )}

              {step === 5 && form && (
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Tries per contact" hint="Before giving up on no answer or busy.">
                    <input className={inputClass} type="number" min={1} max={6} value={form.max_attempts} onChange={num("max_attempts")} />
                  </Field>
                  <Field label="Retry after (minutes)">
                    <input className={inputClass} type="number" min={1} max={1440} value={form.retry_minutes} onChange={num("retry_minutes")} />
                  </Field>
                  <Field label="Calls at once">
                    <input className={inputClass} type="number" min={1} max={10} value={form.max_concurrent} onChange={num("max_concurrent")} />
                  </Field>
                </div>
              )}

              {step === 6 && form && (
                <div className="space-y-4">
                  <div className="grid gap-4 sm:grid-cols-3">
                    <Field label="Start">
                      <Select tall value={startMode} onChange={(e) => setStartMode(e.target.value as "now" | "later")}>
                        <option value="now">Immediately</option>
                        <option value="later">Schedule</option>
                      </Select>
                    </Field>
                    <Field
                      label="Start date"
                      hint={startMode === "now" ? "Calling starts when you press Start calling on the last step." : "It starts calling by itself then."}
                    >
                      <input
                        className={inputClass}
                        type="datetime-local"
                        disabled={startMode === "now"}
                        value={startMode === "now" ? "" : startAt}
                        onChange={(e) => setStartAt(e.target.value)}
                      />
                    </Field>
                    <Field label="Timezone" hint="All calls are placed in Indian time.">
                      <Select tall value="ist" disabled>
                        <option value="ist">India (IST)</option>
                      </Select>
                    </Field>
                    <Field label="Calling window">
                      <div className="flex items-center gap-1.5">
                        <Select tall className="flex-1" value={form.start_hour} onChange={num("start_hour")} aria-label="From">
                          {Array.from({ length: 24 }, (_, h) => (
                            <option key={h} value={h}>
                              {hh(h)}
                            </option>
                          ))}
                        </Select>
                        <span className="text-muted-foreground">–</span>
                        <Select tall className="flex-1" value={form.end_hour} onChange={num("end_hour")} aria-label="To">
                          {Array.from({ length: 24 }, (_, i) => i + 1).map((h) => (
                            <option key={h} value={h} disabled={h <= form.start_hour}>
                              {hh(h)}
                            </option>
                          ))}
                        </Select>
                      </div>
                    </Field>
                    <div>
                      <div className="mb-1.5 text-[13.5px] font-medium">
                        Days <span className="font-normal text-muted-foreground">· {daysText(form.days)}</span>
                      </div>
                      <div className="flex h-10 overflow-hidden rounded-md border border-input">
                        {DAY_NAMES.map((d, i) => {
                          const on = form.days.includes(String(i));
                          return (
                            <button
                              key={d}
                              type="button"
                              aria-pressed={on}
                              onClick={() => toggleDay(i)}
                              className={cn(
                                "flex-1 border-r text-[12.5px] last:border-r-0",
                                on ? "bg-primary text-primary-foreground" : "bg-card text-muted-foreground hover:bg-muted",
                              )}
                            >
                              {d}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                    <Field label="Stop when">
                      <Select tall value={stopWhen} onChange={(e) => setStopWhen(e.target.value as StopWhen)}>
                        <option value="exhausted">Audience exhausted</option>
                        <option value="date">A date is reached</option>
                        <option value="calls">A number of calls is made</option>
                      </Select>
                    </Field>
                  </div>
                  {stopWhen !== "exhausted" && (
                    <div className="grid gap-4 sm:grid-cols-3">
                      <div className="sm:col-start-3">
                        {stopWhen === "date" ? (
                          <Field label="Last calling day" hint="No calls after this day.">
                            <input className={inputClass} type="date" value={last} onChange={(e) => setLast(e.target.value)} />
                          </Field>
                        ) : (
                          <Field label="Stop after (calls)" hint="Calls in one run.">
                            <input className={inputClass} type="number" min={1} value={form.limit || ""} onChange={num("limit")} />
                          </Field>
                        )}
                      </div>
                    </div>
                  )}
                  <label className="flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 text-[13.5px]">
                    <input type="checkbox" className="mt-0.5 size-4 accent-brand" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
                    <span>
                      <b className="font-medium">Also Automatic</b>
                      <span className="text-muted-foreground">
                        : while Automatic calling is on (Store orders page), ring this list&apos;s new contacts as they arrive.
                      </span>
                    </span>
                  </label>
                </div>
              )}

              {step === 7 && form && cfg && (
                <div className="space-y-4">
                  {missingFacts.length > 0 && (
                    <p className="flex gap-2 rounded-md bg-warn-soft px-3 py-2 text-[13px] text-warn">
                      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                      Fill in the campaign details first (step 4): {missingFacts.map((f) => f.label).join(", ")}.
                    </p>
                  )}
                  <dl className="divide-y rounded-md border text-[13.5px]">
                    {(
                      [
                        [1, "Details", `${name || "—"} · ${t?.name ?? type} · budget ${form.budget_rs ? `${fmtRs(form.budget_rs)} a day` : "no cap"}`],
                        [2, "Agent", `${cfg.model_labels[form.model] ?? form.model} · ${form.voice} · ${LANG_LABELS[form.language] ?? form.language}`],
                        [3, "Audience", contacts.data ? (contacts.data.length ? plural(contacts.data.length, "contact") : "No contacts yet") : "…"],
                        [
                          4,
                          "Facts & offers",
                          !t?.facts.length
                            ? "None needed"
                            : [
                                ...t.facts
                                  .filter((f) => f.key !== "offers" && f.key !== "whatsapp_coupon")
                                  .map((f) => `${f.label.replace(" (optional)", "")}: ${(facts[f.key] ?? "").trim() ? "written" : "none"}`),
                                ...(t.facts.some((f) => f.key === "offers")
                                  ? [plural(parseOffers(facts.offers).filter((o) => o.title.trim() || o.details.trim()).length, "offer")]
                                  : []),
                                ...(t.facts.some((f) => f.key === "whatsapp_coupon")
                                  ? [(facts.whatsapp_coupon ?? "").trim() ? `WhatsApp coupon ${facts.whatsapp_coupon}` : "WhatsApp coupon off"]
                                  : []),
                              ].join(" · "),
                        ],
                        [5, "Calling rules", `${tries(form.max_attempts)} · retry after ${form.retry_minutes} min · ${form.max_concurrent} at once`],
                        [
                          6,
                          "Schedule",
                          `${startMode === "now" ? "Starts when you press Start calling" : `Starts ${whenText(startAt) || "— pick a time"}`} · ${hh(form.start_hour)}–${hh(form.end_hour)} IST · ${daysText(form.days)} · stops when ${
                            stopWhen === "date" ? `${last || "—"} passes` : stopWhen === "calls" ? `${form.limit} calls are made` : "everyone is called"
                          }${auto ? " · also Automatic" : ""}`,
                        ],
                      ] as const
                    ).map(([n, label, value]) => (
                      <div key={label} className="flex items-start gap-3 px-3 py-2.5">
                        <dt className="w-30 shrink-0 text-muted-foreground">{label}</dt>
                        <dd className="min-w-0 flex-1 wrap-break-word">{value}</dd>
                        <button type="button" onClick={() => go(n)} className="text-muted-foreground hover:text-foreground" aria-label={`Edit ${label}`}>
                          <Pencil className="size-3.5" />
                        </button>
                      </div>
                    ))}
                  </dl>
                  {!contacts.data?.length && contacts.data && (
                    <p className="text-[12.5px] text-muted-foreground">Add contacts in Audience (step 3) to start or schedule calling.</p>
                  )}
                </div>
              )}

              {!form && step !== 3 && <p className="text-muted-foreground">Loading…</p>}
            </div>
          </Card>

          {error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">{error}</p>}

          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" onClick={() => go(step - 1)} disabled={step === 1 || saving}>
              Back
            </Button>
            {step < STEPS.length ? (
              <Button variant="primary" className="ml-auto" onClick={next} disabled={saving || !type || !form}>
                {saving ? "Saving…" : "Continue"}
              </Button>
            ) : (
              <>
                <Button variant="outline" className="ml-auto" onClick={() => finish(false)} disabled={saving || missingFacts.length > 0}>
                  Save without starting
                </Button>
                <Button variant="primary" onClick={() => finish(true)} disabled={saving || missingFacts.length > 0 || !contacts.data?.length}>
                  {saving ? "Saving…" : startMode === "now" ? "Start calling" : "Schedule"}
                </Button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
