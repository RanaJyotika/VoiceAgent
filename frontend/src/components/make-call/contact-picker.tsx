"use client";

import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { inputClass } from "@/components/ui/dialog";
import { getJson, type ContactHit } from "@/lib/api";
import { fmtPhone, fmtOrder } from "@/lib/format";
import { cn } from "@/lib/utils";

const describe = (c: ContactHit) => [c.order_id ? fmtOrder(c.order_id) : "", c.product ?? "", c.campaign_name ?? "no campaign"].filter(Boolean).join(" · ");

/** Type a name, phone, order number or product; pick one of the matches. The agent reads its order details. */
export function ContactPicker({ value, onChange }: { value: ContactHit | null; onChange: (c: ContactHit | null) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<ContactHit[]>([]);
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState(0);
  const [error, setError] = useState("");
  const box = useRef<HTMLDivElement>(null);

  // Search as you type, a moment after the last key.
  useEffect(() => {
    if (value) return;
    const ctrl = new AbortController();
    const t = setTimeout(() => {
      getJson<ContactHit[]>(`/api/contacts/search?${new URLSearchParams({ q, limit: "20" })}`, ctrl.signal)
        .then((rows) => {
          setHits(rows);
          setSel(0);
          setError("");
        })
        .catch((e) => e.name !== "AbortError" && setError(e.message));
    }, 200);
    return () => {
      clearTimeout(t);
      ctrl.abort();
    };
  }, [q, value]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const pick = (c: ContactHit) => {
    onChange(c);
    setOpen(false);
    setQ("");
  };

  if (value) {
    return (
      <div className="flex min-h-10 items-center gap-3 rounded-md border border-input bg-muted/40 px-3 py-1.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[14px] font-medium">{value.customer_name || "No name"}</div>
          <div className="truncate text-[12px] text-muted-foreground">{describe(value)}</div>
        </div>
        <button
          type="button"
          onClick={() => onChange(null)}
          className="rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          aria-label="Pick another contact"
        >
          <X className="size-4" />
        </button>
      </div>
    );
  }

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setSel((s) => Math.min(hits.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter" && open && hits[sel]) {
      e.preventDefault();
      pick(hits[sel]);
    } else if (e.key === "Escape" && open) {
      e.stopPropagation();
      setOpen(false);
    }
  };

  return (
    <div className="relative" ref={box}>
      <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
      <input
        className={cn(inputClass, "pl-9")}
        placeholder="Search name, phone, order no. or product"
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKey}
        role="combobox"
        aria-expanded={open}
        aria-controls="contact-hits"
      />
      {open && (
        <ul id="contact-hits" role="listbox" className="absolute z-10 mt-1 max-h-[260px] w-full overflow-y-auto rounded-md border bg-card p-1 shadow-lg">
          {error && <li className="px-3 py-2 text-[13px] text-bad">{error}</li>}
          {!error && hits.length === 0 && <li className="px-3 py-2 text-[13px] text-muted-foreground">No contacts match “{q}”.</li>}
          {hits.map((c, i) => (
            <li key={c.id} role="option" aria-selected={i === sel}>
              <button
                type="button"
                onMouseEnter={() => setSel(i)}
                onClick={() => pick(c)}
                className={cn("flex w-full items-baseline gap-3 rounded px-2.5 py-2 text-left", i === sel && "bg-muted")}
              >
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13.5px] font-medium">{c.customer_name || "No name"}</span>
                  <span className="block truncate text-[12px] text-muted-foreground">{describe(c)}</span>
                </span>
                <span className="shrink-0 font-mono text-[12px] text-muted-foreground">{c.phone ? fmtPhone(c.phone) : "no phone"}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
