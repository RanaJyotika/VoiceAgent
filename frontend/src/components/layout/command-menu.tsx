"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CornerDownLeft, Phone, Search, type LucideIcon } from "lucide-react";
import { useMakeCall } from "@/components/make-call/make-call-provider";
import { NAV_ITEMS } from "@/lib/nav";
import { cn } from "@/lib/utils";

type Entry = { label: string; icon: LucideIcon; keywords?: string; run: () => void };

/** Ctrl/⌘ K: type to jump to any page, or start a call. */
export function CommandMenu({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return <Menu onClose={onClose} />;
}

function Menu({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  const { openMakeCall } = useMakeCall();

  const results = useMemo(() => {
    const entries: Entry[] = [
      { label: "Make a call", icon: Phone, keywords: "single call phone test browser dial", run: openMakeCall },
      ...NAV_ITEMS.map((i) => ({
        label: i.label,
        icon: i.icon,
        keywords: i.keywords,
        // Classic pages are outside this app: a full page load.
        run: () => (i.classic ? (window.location.href = i.href) : router.push(i.href)),
      })),
    ];
    const s = q.trim().toLowerCase();
    return entries.filter((i) => !s || `${i.label} ${i.keywords ?? ""}`.toLowerCase().includes(s));
  }, [q, openMakeCall, router]);

  useEffect(() => input.current?.focus(), []);

  const go = (i: number) => {
    const item = results[i];
    if (!item) return;
    onClose();
    item.run();
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(results.length - 1, s + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === "Enter") {
      go(sel);
    } else if (e.key === "Escape") {
      onClose();
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-label="Search"
        className="w-full max-w-[520px] overflow-hidden rounded-xl border bg-card shadow-2xl"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b px-4">
          <Search className="size-4 text-muted-foreground" />
          <input
            ref={input}
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setSel(0);
            }}
            onKeyDown={onKey}
            placeholder="Jump to a page or make a call…"
            className="h-12 flex-1 bg-transparent text-[14.5px] outline-none placeholder:text-muted-foreground"
          />
        </div>
        <ul className="max-h-[320px] overflow-y-auto p-1.5">
          {results.length === 0 && <li className="px-3 py-6 text-center text-muted-foreground">Nothing matches “{q}”.</li>}
          {results.map((item, i) => (
            <li key={item.label}>
              <button
                type="button"
                onMouseEnter={() => setSel(i)}
                onClick={() => go(i)}
                className={cn("flex w-full items-center gap-3 rounded-md px-3 py-2 text-left", i === sel && "bg-muted")}
              >
                <item.icon className="size-4 text-muted-foreground" />
                <span className="flex-1">{item.label}</span>
                {i === sel && <CornerDownLeft className="size-3.5 text-muted-foreground" />}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
