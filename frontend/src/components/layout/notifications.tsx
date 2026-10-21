"use client";

import { useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { AttentionList } from "@/components/overview/attention-list";
import { Button } from "@/components/ui/button";
import { usePoll } from "@/hooks/use-poll";
import type { Overview } from "@/lib/api";

/** The bell: today's "attention required" items, with a red dot when one is serious. */
export function Notifications() {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const items = usePoll<Overview>("/api/overview?range=today", 60_000).data?.attention ?? [];
  const urgent = items.some((i) => i.level !== "info");

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !box.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className="relative" ref={box}>
      <Button variant="ghost" size="icon" onClick={() => setOpen((o) => !o)} aria-label="Notifications" aria-expanded={open}>
        <Bell />
        {urgent && <span className="absolute top-2 right-2 size-2 rounded-full border-2 border-background bg-bad" />}
      </Button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-[min(360px,calc(100vw-24px))] overflow-hidden rounded-lg border bg-card shadow-lg">
          <div className="border-b px-4 py-2.5 text-[13.5px] font-medium">Today</div>
          <AttentionList items={items} />
        </div>
      )}
    </div>
  );
}
