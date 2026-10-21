"use client";

import { useState } from "react";
import { Check, Copy, Eye, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/dialog";
import { Select } from "@/components/ui/select";
import { send } from "@/lib/api";
import { cn } from "@/lib/utils";
import { parseOffers } from "./offers-editor";

type Preview = {
  body: string;
  image: string;
  template: string;
  template_params: string[];
  service: { label: string; ready: boolean; missing: string[] };
  waiting: string;
  template_name: string;
  sends: { slot: string; value: string; from: string }[];
};

/**
 * "Share a coupon on WhatsApp" (owner, 21 Oct 2026): on a cash-on-delivery order, once the customer confirms it, the agent
 * offers one of the campaign's coupons for paying online, and on their yes the code goes to them on WhatsApp.
 * value: the chosen offer's code ("" = off), kept in the campaign's details as whatsapp_coupon.
 */
export function WhatsappCoupon({ value, offers, onChange }: { value: string | undefined; offers: string | undefined; onChange: (code: string) => void }) {
  const coded = parseOffers(offers).filter((o) => o.code.trim() && (o.title.trim() || o.details.trim()));
  const code = (value ?? "").trim().toUpperCase();
  const chosen = coded.find((o) => o.code.trim().toUpperCase() === code);
  const on = !!chosen;
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const toggle = () => {
    setPreview(null);
    onChange(on ? "" : coded[0].code.trim().toUpperCase());
  };

  const show = async () => {
    setError("");
    try {
      setPreview(await send<Preview>("/api/messages/prepaid-preview", "POST", { offer: chosen }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const copy = async () => {
    if (!preview) return;
    await navigator.clipboard.writeText(preview.template);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="rounded-lg border p-4">
      <label className={cn("flex items-start gap-3", coded.length ? "cursor-pointer" : "cursor-not-allowed opacity-70")}>
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-good-soft text-good">
          <MessageCircle className="size-4.5" />
        </span>
        <span className="flex-1">
          <span className="block text-[13.5px] font-medium">Share coupons on WhatsApp</span>
          <span className="block text-[12px] leading-snug text-muted-foreground">
            On a cash on delivery order, once the customer confirms it, the agent asks how they will pay and, if they say cash on delivery, offers this coupon
            once for paying online. If they say yes, the order&apos;s payment changes to Prepaid (noted on the call, and tagged and noted on the order in
            Shopify) and the code goes to them on WhatsApp from your business number.
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={on}
          disabled={!coded.length}
          onClick={toggle}
          className={cn("relative mt-1 h-6 w-11 shrink-0 rounded-full transition-colors", on ? "bg-primary" : "bg-input")}
        >
          <span className={cn("absolute top-0.5 size-5 rounded-full bg-card shadow transition-[left]", on ? "left-5.5" : "left-0.5")} />
        </button>
      </label>

      {!coded.length && <p className="mt-3 text-[12px] text-muted-foreground">Add an offer with an Offer code above first; that code is what gets sent.</p>}

      {on && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <Field label="Coupon to send">
              <Select
                tall
                value={chosen.code.trim().toUpperCase()}
                onChange={(e) => {
                  setPreview(null);
                  onChange(e.target.value);
                }}
              >
                {coded.map((o) => (
                  <option key={o.code} value={o.code.trim().toUpperCase()}>
                    {o.code.trim().toUpperCase()} · {o.title.trim() || o.details.trim().slice(0, 40)}
                    {o.max_discount.trim() ? ` · ${o.max_discount.trim()} off` : ""}
                  </option>
                ))}
              </Select>
            </Field>
            <Button variant="outline" onClick={show}>
              <Eye />
              See the message
            </Button>
          </div>
          <p className="-mt-1.5 text-[12px] text-muted-foreground">The agent says the discount from the offer&apos;s Max discount (e.g. 10%).</p>
          {!chosen.max_discount.trim() && (
            <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
              This offer has no Max discount, so the agent and the message describe it by its name instead of a number.
            </p>
          )}
          {error && <p className="text-[12.5px] text-bad">{error}</p>}
          {preview && (
            <div className="grid gap-3 lg:grid-cols-2">
              <div>
                <div className="mb-1.5 text-[12.5px] font-medium text-muted-foreground">What a customer gets (sample customer)</div>
                <div className="rounded-lg bg-[#e7ddd3] p-3 dark:bg-[#0b141a]">
                  <div className="max-w-[92%] rounded-lg rounded-tl-none bg-white px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap text-[#111b21] shadow-sm dark:bg-[#202c33] dark:text-[#e9edef]">
                    {preview.image && (
                      // eslint-disable-next-line @next/next/no-img-element -- a store photo by its own address, in a static export
                      <img src={preview.image} alt="" className="-mx-1 mb-2 aspect-square w-[calc(100%+0.5rem)] rounded-md object-cover" />
                    )}
                    {whatsappBold(preview.body)}
                  </div>
                </div>
              </div>
              <div>
                <div className="mb-1.5 flex items-center text-[12.5px] font-medium text-muted-foreground">
                  Template to submit in Pinnacle
                  <Button size="sm" variant="ghost" className="ml-auto" onClick={copy}>
                    {copied ? <Check /> : <Copy />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
                <pre className="overflow-auto rounded-lg border bg-muted/30 p-3 font-sans text-[12.5px] leading-relaxed whitespace-pre-wrap">
                  {preview.template}
                </pre>
                <p className="mt-1.5 text-[12px] text-muted-foreground">
                  Variables, in order: {preview.template_params.map((p, i) => `{{${i + 1}}} ${p}`).join(", ")}.
                </p>
              </div>
              {preview.template_name && (
                <div className="rounded-lg border p-3 lg:col-span-2">
                  <div className="text-[12.5px] font-medium">
                    Customers get your approved template <span className="font-mono">{preview.template_name}</span>, filled with:
                  </div>
                  <p className="mb-2 text-[12px] text-muted-foreground">Its own wording is what they read; the message above is only a suggestion.</p>
                  <dl className="grid gap-1 text-[12.5px]">
                    {preview.image && (
                      <div className="flex gap-3">
                        <dt className="w-24 shrink-0 text-muted-foreground">Image</dt>
                        <dd className="min-w-0 truncate">{preview.image}</dd>
                      </div>
                    )}
                    {preview.sends.map((s) => (
                      <div key={s.slot} className="flex gap-3">
                        <dt className="w-24 shrink-0 font-mono text-muted-foreground">{s.slot}</dt>
                        <dd className="min-w-0 truncate">
                          {s.value} <span className="text-muted-foreground">({s.from})</span>
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
              <p
                className={cn(
                  "rounded-md px-3 py-2 text-[12.5px] lg:col-span-2",
                  preview.service.ready && !preview.waiting ? "bg-good-soft text-good" : "bg-warn-soft text-warn",
                )}
              >
                {preview.service.ready && !preview.waiting
                  ? `Sending is set up (${preview.service.label}).`
                  : `Not sending yet: ${[
                      !preview.service.ready && `missing in .env: ${preview.service.missing.join(", ") || "a message service (MESSAGE_PROVIDER)"}`,
                      preview.waiting.replace(/; send it by hand meanwhile$/, ""),
                    ]
                      .filter(Boolean)
                      .join("; ")}. Until then the messages wait in the outbox and can be sent by hand.`}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** WhatsApp's *bold* as bold. */
function whatsappBold(text: string) {
  return text
    .split(/(\*[^*\n]+\*)/g)
    .map((part, i) => (part.startsWith("*") && part.endsWith("*") && part.length > 2 ? <b key={i}>{part.slice(1, -1)}</b> : <span key={i}>{part}</span>));
}
