"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, Headphones, Mic, MicOff, PhoneCall, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, Field, inputClass } from "@/components/ui/dialog";
import { Select } from "@/components/ui/select";
import { getJson, type AppConfig, type ContactHit, type PhoneLine } from "@/lib/api";
import { fmtPhone, fmtOrder } from "@/lib/format";
import { cn } from "@/lib/utils";
import { LANG_LABELS, fitVoice, remember, remembered, voicesFor } from "@/lib/voices";
import { ContactPicker } from "./contact-picker";
import { useBrowserCall } from "./use-browser-call";

type Step = "form" | "confirm" | "dialed" | "test";

/** A 10-digit Indian mobile number: what the phone box accepts and what it sends. */
const MOBILE = /^[6-9]\d{9}$/;
const tenDigits = (raw: string | null | undefined) => (raw ?? "").replace(/\D/g, "").slice(-10);

/** What the phone box keeps of what was typed or pasted: digits only, "+91 98765 43210" and "098765 43210" as 9876543210. */
function phoneDigits(raw: string) {
  let d = raw.replace(/\D/g, "");
  if (d.length > 10 && d.startsWith("91")) d = d.slice(2);
  if (d.length > 10 && d.startsWith("0")) d = d.slice(1);
  return d.slice(0, 10);
}

/** How the call type of a picked contact is called on screen, for "empty reason = the usual script". */
const SCRIPT_NAMES: Record<string, string> = {
  order_confirmation: "order confirmation",
  booking_confirmation: "booking confirmation",
  campaign_invite: "campaign invite",
  abandoned_cart: "abandoned cart",
};

export function MakeCallDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [line, setLine] = useState<PhoneLine | null>(null);
  const [loadError, setLoadError] = useState("");
  const [step, setStep] = useState<Step>("form");
  const [phone, setPhone] = useState(""); // digits only, at most 10
  const [phoneTouched, setPhoneTouched] = useState(false);
  const [contact, setContact] = useState<ContactHit | null>(null);
  const [name, setName] = useState(""); // for a number with no contact
  const [reason, setReason] = useState("");
  const [model, setModel] = useState("");
  const [voice, setVoice] = useState("");
  const [language, setLanguage] = useState("hinglish");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dialed, setDialed] = useState("");
  // The contact made for a typed number, reused while the number, name and reason stay the same.
  const madeFor = useRef<{ key: string; id: number } | null>(null);
  const call = useBrowserCall();

  // Fresh settings each time it opens: the build changes on every server restart, the phone line with the tunnel.
  useEffect(() => {
    if (!open) return;
    const ctrl = new AbortController();
    Promise.all([getJson<AppConfig>("/api/config", ctrl.signal), getJson<PhoneLine>("/api/plivo/status", ctrl.signal)])
      .then(([c, l]) => {
        setCfg(c);
        setLine(l);
        setLoadError("");
        // A single call always starts on the default agent: remembering the last one once put real calls on
        // ChatGPT without anyone noticing (calls 103, 104).
        setModel(c.model);
        setVoice(fitVoice(c, c.model, remembered("voice") || c.voice));
        const lang = remembered("language");
        setLanguage(c.languages.includes(lang) ? lang : "hinglish");
      })
      .catch((e) => e.name !== "AbortError" && setLoadError(e.message));
    return () => ctrl.abort();
  }, [open]);

  const voices = useMemo(() => (cfg ? voicesFor(cfg, model) : []), [cfg, model]);
  const phoneOk = MOBILE.test(phone);
  const why = reason.trim();
  const agentLabel = cfg?.model_labels[model] ?? model;
  const usualScript = contact ? (SCRIPT_NAMES[contact.call_type ?? ""] ?? "order confirmation") : "";

  // What still stops a call, in the order the form asks for it; "" when it can go.
  const missing = !phoneOk
    ? "Enter the 10-digit mobile number."
    : !contact && !why
      ? "Write the reason for the call, or pick a customer."
      : !cfg
        ? "Loading…"
        : "";
  const lineOff = line && !line.ready ? `Phone calls are off (missing: ${line.missing.join(", ")}). Start the server with run_phone.bat to call.` : "";

  const close = () => {
    if (call.running) call.hangup();
    onClose();
    setStep("form");
    setError("");
  };

  const pickContact = (c: ContactHit | null) => {
    setContact(c);
    if (c?.phone) setPhone(tenDigits(c.phone));
    setError("");
  };

  /** The contact this call reads from: the picked one, or one made now for the typed number and reason. */
  const contactId = async () => {
    if (contact) return contact.id;
    const key = JSON.stringify([phone, name.trim(), why]);
    if (madeFor.current?.key === key) return madeFor.current.id;
    const r = await fetch("/api/contacts/single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ phone, name: name.trim(), reason: why }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `${r.status} ${r.statusText}`);
    madeFor.current = { key, id: j.id };
    return j.id as number;
  };

  const placeCall = async () => {
    if (!cfg) return;
    setBusy(true);
    setError("");
    try {
      const id = await contactId();
      const qs = new URLSearchParams({ voice, model, to: phone, language, build: cfg.build, reason: why });
      const r = await fetch(`/api/calls/dial/${id}?${qs}`, { method: "POST" });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `${r.status} ${r.statusText}`);
      remember("voice", voice);
      setDialed(j.dialed || phone);
      setStep("dialed");
    } catch (e) {
      setError(`Could not place the call: ${(e as Error).message}`);
      setStep("form");
    } finally {
      setBusy(false);
    }
  };

  const startTest = async () => {
    if (!cfg) return;
    setError("");
    try {
      const id = await contactId();
      remember("voice", voice);
      setStep("test");
      call.start({ contactId: id, voice, model, language, build: cfg.build, reason: why });
    } catch (e) {
      setError(`Could not start the test: ${(e as Error).message}`);
    }
  };

  const title = step === "test" ? "Browser test" : step === "confirm" ? "Place a real call?" : "Make a call";
  const description =
    step === "test"
      ? `${contact?.customer_name || name || "Customer"} · ${agentLabel} · ${voice}. Use headphones so the mic does not hear the speaker.`
      : step === "confirm"
        ? "This rings a real phone. Every minute is paid (phone line plus the AI agent)."
        : step === "dialed"
          ? undefined
          : "Place a single call now, or test it free in the browser.";

  const phoneBad = phoneTouched && phone !== "" && !phoneOk;

  return (
    <Dialog open={open} onClose={close} title={title} description={description} className={step === "test" ? "max-w-[560px]" : undefined}>
      {loadError && <p className="mb-4 rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">Could not reach the server: {loadError}</p>}

      {step === "form" && (
        <div className="space-y-4">
          <Field
            label="Phone number"
            required
            hint={
              phoneBad ? (
                <span className="text-bad">A mobile number is 10 digits and starts with 6, 7, 8 or 9.</span>
              ) : line?.dial_only_my_number ? (
                "Test mode is on: the call rings your own number (PHONE_NUMBER in .env), whatever is typed here."
              ) : undefined
            }
          >
            <div
              className={cn(
                "flex h-10 items-center rounded-md border border-input bg-card focus-within:border-brand focus-within:ring-2 focus-within:ring-brand/20",
                phoneBad && "border-bad focus-within:border-bad focus-within:ring-bad/20",
              )}
            >
              <span className="border-r px-3 font-mono text-[14px] text-muted-foreground select-none">+91</span>
              <input
                className="h-full min-w-0 flex-1 bg-transparent px-3 font-mono text-[14px] tracking-wide outline-none placeholder:font-sans placeholder:tracking-normal placeholder:text-subtle-foreground"
                placeholder="98xxx xxxxx"
                inputMode="numeric"
                autoComplete="tel-national"
                required
                aria-invalid={phoneBad}
                value={phone}
                onChange={(e) => setPhone(phoneDigits(e.target.value))}
                onBlur={() => setPhoneTouched(true)}
              />
            </div>
          </Field>

          <Field
            label="Customer"
            optional
            hint={contact ? "The agent uses this customer's order details on the call." : "Pick a customer to use their order details."}
          >
            <ContactPicker value={contact} onChange={pickContact} />
          </Field>

          {!contact && (
            <Field label="Customer name" optional>
              <input className={inputClass} placeholder="How the agent greets them" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
            </Field>
          )}

          <Field
            label="Reason for the call"
            required={!contact}
            optional={!!contact}
            hint={
              contact && !why
                ? `Empty: the usual ${usualScript} call. Written: the agent calls about this instead.`
                : "The agent says this to the customer in its own words, answers from it, and notes their reply."
            }
          >
            <textarea
              className={cn(inputClass, "h-auto min-h-[84px] resize-y py-2 leading-snug")}
              placeholder="e.g. Aapka order #75942 Diwali ki wajah se 2 din late aayega. Puchiye ki kya woh abhi bhi order chahte hain."
              maxLength={1000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Agent">
              <Select
                tall
                value={model}
                onChange={(e) => {
                  setModel(e.target.value);
                  if (cfg) setVoice(fitVoice(cfg, e.target.value, voice));
                }}
              >
                {cfg?.models.map((m) => (
                  <option key={m} value={m}>
                    {cfg.model_labels[m] ?? m}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Voice">
              <Select tall value={voice} onChange={(e) => setVoice(e.target.value)}>
                {voices.map(([n, gender]) => (
                  <option key={n} value={n}>
                    {n} ({gender})
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Language">
              <Select
                tall
                value={language}
                onChange={(e) => {
                  setLanguage(e.target.value);
                  remember("language", e.target.value);
                }}
              >
                {(cfg?.languages ?? ["hinglish", "hindi", "english"]).map((l) => (
                  <option key={l} value={l}>
                    {LANG_LABELS[l] ?? l}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Caller ID">
              <input className={cn(inputClass, "bg-muted/40")} readOnly value={line ? (line.from_number ? fmtPhone(line.from_number) : "Not set") : "…"} />
            </Field>
          </div>

          {contact && <OrderDetails c={contact} />}

          {error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">{error}</p>}

          {(missing || lineOff) && (
            <p className="flex gap-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>
                {missing || lineOff}
                {!missing && lineOff ? " The browser test still works." : ""}
              </span>
            </p>
          )}

          <div className="flex flex-wrap items-center justify-end gap-2 pt-1">
            <Button variant="ghost" className="mr-auto" disabled={!!missing} onClick={startTest} title="Free: talk to the agent through this computer's mic">
              <Headphones />
              Test in browser
            </Button>
            <Button
              variant="primary"
              size="md"
              disabled={!!missing || !!lineOff}
              onClick={() => setStep("confirm")}
              title={missing || lineOff || "Places a real, paid phone call"}
            >
              Call now
            </Button>
          </div>
        </div>
      )}

      {step === "confirm" && (
        <div className="space-y-4">
          <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-2 rounded-lg border bg-muted/30 p-4 text-[13.5px]">
            <dt className="text-muted-foreground">Number</dt>
            <dd className="font-mono">{line?.dial_only_my_number ? "your own number (test mode)" : fmtPhone(phone)}</dd>
            <dt className="text-muted-foreground">Customer</dt>
            <dd>
              {contact ? contact.customer_name || "No name" : name.trim() || "Not given"}
              {contact?.order_id ? ` (${fmtOrder(contact.order_id)})` : ""}
            </dd>
            <dt className="text-muted-foreground">About</dt>
            <dd className="break-words">{why || `the usual ${usualScript} call`}</dd>
            <dt className="text-muted-foreground">Agent</dt>
            <dd>
              {agentLabel} · {voice} · {LANG_LABELS[language] ?? language}
            </dd>
            <dt className="text-muted-foreground">Caller ID</dt>
            <dd className="font-mono">{line?.from_number ? fmtPhone(line.from_number) : "—"}</dd>
          </dl>
          <p className="text-[12.5px] text-muted-foreground">Only call people who expect it.</p>
          {error && <p className="rounded-md bg-bad-soft px-3 py-2 text-[13px] text-bad">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setStep("form")} disabled={busy}>
              Back
            </Button>
            <Button variant="primary" onClick={placeCall} disabled={busy}>
              <PhoneCall />
              {busy ? "Dialing…" : "Yes, call now"}
            </Button>
          </div>
        </div>
      )}

      {step === "dialed" && (
        <div className="space-y-4 text-center">
          <CheckCircle2 className="mx-auto size-10 text-good" />
          <div>
            <div className="text-[16px] font-medium">Dialing {fmtPhone(dialed)}…</div>
            <p className="mt-1 text-[13.5px] text-muted-foreground">Follow it in Live calls on the Overview. The result lands in Calls when it ends.</p>
          </div>
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        </div>
      )}

      {step === "test" && <TestConsole call={call} onBack={() => setStep("form")} />}
    </Dialog>
  );
}

/** What the agent will read out, in place of the design's "Variables" box: the contact's own order data. */
function OrderDetails({ c }: { c: ContactHit }) {
  const rows: [string, string | null][] = [
    ["Order", c.order_id ? fmtOrder(c.order_id) : null],
    ["Product", c.product],
    ["Quantity", c.quantity],
    ["Amount", c.amount ? `₹${c.amount}` : null],
    ["Payment", c.payment_mode],
    ["Address", c.address],
    ["Campaign", c.campaign_name],
  ];
  return (
    <div>
      <div className="mb-1.5 text-[13.5px] font-medium">Order details</div>
      <dl className="max-h-[132px] overflow-y-auto rounded-md border bg-muted/30 px-3 py-2 font-mono text-[12.5px] leading-relaxed">
        {rows
          .filter(([, v]) => v)
          .map(([k, v]) => (
            <div key={k} className="flex gap-2">
              <dt className="w-[72px] shrink-0 text-muted-foreground">{k}</dt>
              <dd className="min-w-0 break-words">{v}</dd>
            </div>
          ))}
      </dl>
    </div>
  );
}

function TestConsole({ call, onBack }: { call: ReturnType<typeof useBrowserCall>; onBack: () => void }) {
  const [typed, setTyped] = useState("");
  const log = useRef<HTMLDivElement>(null);

  useEffect(() => {
    log.current?.scrollTo({ top: log.current.scrollHeight });
  }, [call.lines]);

  const send = () => call.sendText(typed) && setTyped("");

  return (
    <div className="space-y-3">
      <div className="rounded-md bg-muted/50 px-3 py-2 text-[13px]">{call.status || "…"}</div>

      <div className="flex items-center gap-3 text-[12.5px] text-muted-foreground">
        <Mic className="size-4" />
        <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
          <div className="h-full rounded-full bg-brand transition-[width] duration-100" style={{ width: `${call.muted ? 0 : call.level}%` }} />
        </div>
        <button
          type="button"
          onClick={call.toggleMute}
          className="inline-flex items-center gap-1.5 rounded px-1.5 py-0.5 hover:bg-muted hover:text-foreground"
          disabled={!call.running}
        >
          {call.muted ? <MicOff className="size-3.5" /> : <Mic className="size-3.5" />}
          {call.muted ? "Muted: type replies" : "Mute mic"}
        </button>
      </div>

      <div ref={log} className="h-[260px] space-y-2 overflow-y-auto rounded-md border p-3 text-[13.5px]">
        {call.lines.length === 0 && <p className="text-muted-foreground">The conversation shows here.</p>}
        {call.lines.map((l) => (
          <p
            key={l.id}
            className={cn(
              l.role === "meta" && "text-[12px] text-muted-foreground italic",
              l.role === "error" && "text-bad",
              l.role === "customer" && "text-info",
            )}
          >
            {l.role === "agent" && <b className="font-medium">Agent: </b>}
            {l.role === "customer" && <b className="font-medium">Customer: </b>}
            {l.text}
          </p>
        ))}
      </div>

      <div className="flex gap-2">
        <input
          className={inputClass}
          placeholder="Or type what the customer says and press Enter"
          value={typed}
          disabled={!call.connected}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <Button variant="outline" size="icon" className="h-10 w-10" onClick={send} disabled={!call.connected || !typed.trim()} aria-label="Send">
          <Send />
        </Button>
      </div>

      <div className="flex justify-end gap-2 pt-1">
        {call.running ? (
          <Button variant="primary" className="bg-bad text-white" onClick={call.hangup}>
            End test
          </Button>
        ) : (
          <Button variant="outline" onClick={onBack}>
            Back
          </Button>
        )}
      </div>
    </div>
  );
}
