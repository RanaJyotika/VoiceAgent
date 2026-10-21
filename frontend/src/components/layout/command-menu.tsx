"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { CornerDownLeft, Search } from "lucide-react";
import { NAV_ITEMS } from "@/lib/nav";
import { cn } from "@/lib/utils";

/** Ctrl/⌘ K: type to jump to any page. */
export function CommandMenu({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return <Menu onClose={onClose} />;
}

function Menu({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  const results = useMemo(() => {
    const s = q.trim().toLowerCase();
    return NAV_ITEMS.filter((i) => !s || `${i.label} ${i.keywords ?? ""}`.toLowerCase().includes(s));
  }, [q]);

  useEffect(() => input.current?.focus(), []);

  const go = (i: number) => {
    const item = results[i];
    if (!item) return;
    onClose();
    if (item.classic) window.location.href = item.href;
    else router.push(item.href);
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
            placeholder="Jump to a page…"
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
