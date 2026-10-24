"use client";

import { useEffect, useRef, useState } from "react";
import { CalendarClock, MessageSquare, Phone, X } from "lucide-react";
import { CallPanel } from "@/components/overview/call-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { usePoll } from "@/hooks/use-poll";
import { useSidePanel } from "@/hooks/use-side-panel";
import { send, type OrderDetail } from "@/lib/api";
import { fmtClock } from "@/lib/format";
import { outcomeOf } from "@/lib/outcomes";
import { cn } from "@/lib/utils";
import { Items, TagChip, rupees, toneOf, when } from "./shared";

type Props = { name: string; onClose: () => void; onCall: () => void; onChanged: () => void };

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-b px-4 py-4 last:border-b-0">
      <h3 className="mb-2.5 text-[12px] font-medium tracking-wide text-muted-foreground uppercase">{title}</h3>
      {children}
    </section>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  if (v === "" || v == null) return null;
  return (
    <div className="flex gap-3 py-0.5 text-[13px]">
      <dt className="w-28 shrink-0 text-muted-foreground">{k}</dt>
      <dd className="min-w-0 break-words">{v}</dd>
    </div>
  );
}

/** One order on the right: its call, the customer, the products, notes; call now or book a time. */
export function OrderPanel({ name, onClose, onCall, onChanged }: Props) {
  const { toast } = useFeedback();
  const { data: o, error, reload } = usePoll<OrderDetail>(`/api/order?${new URLSearchParams({ name })}`, 10_000);
  const [at, setAt] = useState("");
  const [note, setNote] = useState("");
  const [transcript, setTranscript] = useState<number | null>(null);
  const panel = useRef<HTMLElement>(null);
  // While a call's conversation is open over it, a click outside closes that one first.
  useSidePanel(panel, 460, onClose, transcript == null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && transcript == null && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose, transcript]);

  const schedule = async () => {
    if (!at) return toast("Pick a date and time first.", true);
    try {
      await send("/api/order/schedule", "POST", { name, at });
      toast("Scheduled. Automatic calling rings it then.");
      setAt("");
      reload();
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const addNote = async () => {
    try {
      await send("/api/order/note", "POST", { name, text: note });
      setNote("");
      reload();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const st = o?.store_status;
  return (
    <>
      <aside
        ref={panel}
        aria-label={`Order ${name}`}
        className="side-panel fixed inset-y-0 right-0 z-40 flex w-full max-w-[460px] flex-col border-l bg-card shadow-2xl sm:top-[52px] sm:shadow-xl"
      >
        <header className="border-b px-4 pt-3.5 pb-3">
          <div className="flex items-start gap-3">
            <div className="min-w-0 flex-1">
              <div className="truncate text-[16px] font-semibold">
                Order <span className="font-mono">{name}</span>
              </div>
              <div className="mt-0.5 truncate text-[12.5px] text-muted-foreground">
                {o ? [o.customer || "No name", o.phone ? `+91 ${o.phone}` : "", when(o.created_at)].filter(Boolean).join(" · ") : ""}
              </div>
            </div>
            <button type="button" onClick={onClose} aria-label="Close" className="rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground">
              <X className="size-4" />
            </button>
          </div>
          {o && (
            <div className="mt-2.5 flex flex-wrap gap-1">
              {o.tags.map((t) => (
                <TagChip key={t.label} tag={t} />
              ))}
            </div>
          )}
        </header>

        <div className="flex-1 overflow-y-auto">
          {error && !o && <p className="p-4 text-bad">Could not load the order: {error}</p>}
          {!o && !error && <p className="p-4 text-muted-foreground">Loading…</p>}
          {o && (
            <>
              <Section title="Call">
                <div className="mb-2">
                  <Badge tone={toneOf(o.state_tone)} dot>
                    {o.state_label}
                  </Badge>
                </div>
                <dl>
                  <Row k="Tries" v={o.attempts ? String(o.attempts) : ""} />
                  <Row k="Result" v={o.result} />
                  <Row k="Next call" v={when(o.next_try_at)} />
                </dl>
                {o.calls.length > 0 && (
                  <div className="mt-3 divide-y rounded-md border">
                    {o.calls.map((c) => {
                      const r = outcomeOf(c.outcome || (c.error ? "error" : c.seconds ? "none" : "no_answer"));
                      return (
                        <div key={c.id} className="flex items-center gap-2 px-3 py-2 text-[13px]">
                          {r && <Badge tone={r.tone}>{r.label}</Badge>}
                          <span className="font-mono text-[12px] text-muted-foreground tabular">{fmtClock(c.seconds)}</span>
                          <span className="min-w-0 flex-1 truncate text-[12px] text-muted-foreground">
                            {when(c.at)}
                            {c.channel === "browser" ? " · browser test" : ""}
                          </span>
                          <Button size="sm" variant="ghost" onClick={() => setTranscript(c.id)}>
                            <MessageSquare />
                            View
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                )}
                <div className="mt-3 flex gap-2">
                  <input
                    type="datetime-local"
                    aria-label="Call at"
                    className={cn(inputClass, "h-9 flex-1")}
                    value={at}
                    onChange={(e) => setAt(e.target.value)}
                  />
                  <Button onClick={schedule}>
                    <CalendarClock />
                    Schedule
                  </Button>
                </div>
              </Section>

              <Section title="Customer">
                <div className="font-medium">{o.customer || "—"}</div>
                <dl className="mt-1">
                  <Row k="Phone" v={o.phone ? <span className="font-mono">+91 {o.phone}</span> : ""} />
                  <Row k="Email" v={o.email} />
                  <Row k="Earlier orders" v={String(o.earlier_orders)} />
                  <Row k="Address" v={o.address} />
                </dl>
              </Section>

              <Section title="Order">
                <Items items={o.items} size={44} />
                <dl className="mt-3">
                  <Row k="Amount" v={<span className="font-medium">{rupees(o.amount)}</span>} />
                  <Row k="Payment" v={o.payment} />
                  <Row k="Placed" v={when(o.created_at)} />
                  <Row k="Delivery" v={st?.delivery} />
                  <Row k="Courier" v={st?.courier} />
                  <Row k="Tracking" v={st?.tracking ? <span className="font-mono">{st.tracking}</span> : ""} />
                </dl>
              </Section>

              <Section title="Notes">
                {o.notes.length > 0 && (
                  <div className="mb-3 space-y-2">
                    {o.notes.map((n, i) => (
                      <div key={i} className="rounded-md bg-muted/50 px-3 py-2 text-[13px]">
                        <div className="whitespace-pre-wrap">{n.text}</div>
                        <div className="mt-1 text-[11.5px] text-muted-foreground">{when(n.created_at)}</div>
                      </div>
                    ))}
                  </div>
                )}
                <textarea
                  className={cn(inputClass, "h-auto min-h-18 resize-y py-2 leading-snug")}
                  placeholder="A note for the team"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                />
                <div className="mt-2 flex justify-end">
                  <Button size="sm" onClick={addNote} disabled={!note.trim()}>
                    Add note
                  </Button>
                </div>
              </Section>
            </>
          )}
        </div>

        <footer className="border-t px-4 py-3">
          <Button variant="primary" className="w-full justify-center" disabled={!o || o.state === "calling"} onClick={onCall}>
            <Phone />
            {o?.state === "calling" ? "Calling…" : o?.calls.length ? "Call again" : "Call now"}
          </Button>
        </footer>
      </aside>
      {transcript != null && <CallPanel key={transcript} callId={transcript} onClose={() => setTranscript((t) => (t === transcript ? null : t))} />}
    </>
  );
}
