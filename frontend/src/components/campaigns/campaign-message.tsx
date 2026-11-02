"use client";

import { useState } from "react";
import { Eye, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Field, inputClass } from "@/components/ui/dialog";
import { send } from "@/lib/api";
import { cn } from "@/lib/utils";

/** The campaign's own WhatsApp message as kept in its details (call_types.campaign_message reads the same). */
export type CampaignMessageValue = {
  on: boolean;
  template: string;
  language: string;
  line1: string;
  line2: string;
  line3: string;
  link: string;
  image: string;
  button: boolean;
};

const EMPTY: CampaignMessageValue = { on: false, template: "", language: "en", line1: "", line2: "", line3: "", link: "", image: "", button: true };

export function parseMessage(raw: string | undefined): CampaignMessageValue {
  try {
    const v = JSON.parse(raw || "{}");
    return { ...EMPTY, ...(v && typeof v === "object" && !Array.isArray(v) ? v : {}) };
  } catch {
    return EMPTY;
  }
}

type Preview = {
  body: string;
  image: string;
  service: { label: string; ready: boolean; missing: string[] };
  waiting: string;
  template_name: string;
  sends: { slot: string; value: string; from: string }[];
};

/**
 * "WhatsApp message after the call" (owner, 27 Oct 2026): a campaign invite sends its own wording after a call the
 * customer picked up and did not turn down (owner, 28 Oct 2026; live_bridge.MESSAGE_RESULTS). The three lines, the image
 * and the link are set here. value: the JSON kept in the campaign's details as whatsapp_message.
 */
export function CampaignMessage({ value, offers, onChange }: { value: string | undefined; offers: string | undefined; onChange: (json: string) => void }) {
  const msg = parseMessage(value);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const put = (next: Partial<CampaignMessageValue>) => {
    setPreview(null);
    onChange(JSON.stringify({ ...msg, ...next }));
  };
  const text = (k: keyof CampaignMessageValue) => ({
    value: String(msg[k] ?? ""),
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => put({ [k]: e.target.value }),
  });

  const show = async () => {
    setError("");
    try {
      setPreview(await send<Preview>("/api/messages/campaign-preview", "POST", { message: msg, offers }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className="rounded-lg border p-4">
      <label className="flex cursor-pointer items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-md bg-good-soft text-good">
          <MessageCircle className="size-4.5" />
        </span>
        <span className="flex-1">
          <span className="block text-[13.5px] font-medium">WhatsApp message after the call</span>
          <span className="block text-[12px] leading-snug text-muted-foreground">
            Only after a call the customer picked up and did not say no to: interested, wants help, call back later, unclear, or hung up without answering.
            Never after no answer, busy, not interested or already booked, nor to a number that asked not to be called, a wrong number, a complaint or abuse.
            The agent tells interested customers the link comes on WhatsApp.
          </span>
        </span>
        <button
          type="button"
          role="switch"
          aria-checked={msg.on}
          onClick={() => put({ on: !msg.on })}
          className={cn("relative mt-1 h-6 w-11 shrink-0 rounded-full transition-colors", msg.on ? "bg-primary" : "bg-input")}
        >
          <span className={cn("absolute top-0.5 size-5 rounded-full bg-card shadow transition-[left]", msg.on ? "left-5.5" : "left-0.5")} />
        </button>
      </label>

      {msg.on && (
        <div className="mt-4 space-y-3 border-t pt-4">
          <p className="text-[12px] leading-snug text-muted-foreground">
            Sent on the store&apos;s WhatsApp template, the same one as the order and cart messages: a picture, your three lines, and a Details button.
          </p>
          {(["line1", "line2", "line3"] as const).map((k, i) => (
            <Field key={k} label={`Line ${i + 1}: fills {{${i + 1}}}`} required>
              <input
                className={inputClass}
                placeholder={
                  ["Namaste {name}, Baglamukhi Yantra puja ka nimantran 🙏", "Shatru aur nazar se raksha ke liye, pandit ji aapke naam se puja karenge", "Book karne ke liye neeche button dabaiye 👇"][i]
                }
                {...text(k)}
              />
            </Field>
          ))}
          <p className="-mt-1 text-[12px] text-muted-foreground">
            In the lines you can write {"{name}"} (the customer, with “ji”), {"{link}"}, and {"{code}"} / {"{discount}"} (the first offer above with a code). One
            line each: WhatsApp takes no line breaks inside a value.
          </p>
          <Field
            label="Link"
            required
            hint="A vedicshop.store page opens from the Details button. A link on any other site (vedicvaibhav.com) is put at the end of line 3, unless you place it yourself with {link}; the button then opens the store's products."
          >
            <input className={inputClass} placeholder="https://vedicvaibhav.com/product/…" {...text("link")} />
          </Field>
          <Field label="Image link" optional hint="The picture at the top (https, JPG or PNG). Empty: the store's default image.">
            <input className={inputClass} placeholder="https://…/baglamukhi.jpg" {...text("image")} />
          </Field>
          <div className="flex justify-end">
            <Button variant="outline" onClick={show}>
              <Eye />
              See the message
            </Button>
          </div>
          {error && <p className="text-[12.5px] text-bad">{error}</p>}
          {preview && (
            <div className="grid gap-3 lg:grid-cols-2">
              <div>
                <div className="mb-1.5 text-[12.5px] font-medium text-muted-foreground">Your lines (sample customer)</div>
                <div className="rounded-lg bg-[#e7ddd3] p-3 dark:bg-[#0b141a]">
                  <div className="max-w-[92%] rounded-lg rounded-tl-none bg-white px-3 py-2 text-[13px] leading-relaxed whitespace-pre-wrap text-[#111b21] shadow-sm dark:bg-[#202c33] dark:text-[#e9edef]">
                    {preview.image && (
                      // eslint-disable-next-line @next/next/no-img-element -- an image by its own address, in a static export
                      <img src={preview.image} alt="" className="-mx-1 mb-2 aspect-square w-[calc(100%+0.5rem)] rounded-md object-cover" />
                    )}
                    {preview.body || "—"}
                  </div>
                </div>
              </div>
              <div className="rounded-lg border p-3">
                <div className="text-[12.5px] font-medium">
                  Template <span className="font-mono">{preview.template_name || "—"}</span> gets:
                </div>
                <p className="mb-2 text-[12px] text-muted-foreground">The template&apos;s own wording is what customers read around these values.</p>
                <dl className="grid gap-1 text-[12.5px]">
                  {preview.sends.map((s) => (
                    <div key={s.slot} className="flex gap-3">
                      <dt className="w-24 shrink-0 font-mono text-muted-foreground">{s.slot}</dt>
                      <dd className="min-w-0 break-all">{s.value}</dd>
                    </div>
                  ))}
                </dl>
              </div>
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
