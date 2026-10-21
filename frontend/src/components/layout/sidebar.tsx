"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AudioLines } from "lucide-react";
import { usePoll } from "@/hooks/use-poll";
import { fmtRs } from "@/lib/format";
import { NAV, type NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";

type Stats = { cost_rs: number; calls: number };
type Auto = { enabled: boolean; running: boolean; settings: { budget_rs: number } };

function NavLink({ item, active, onNavigate }: { item: NavItem; active: boolean; onNavigate: () => void }) {
  const cls = cn(
    "flex h-9 items-center gap-3 rounded-md px-2.5 text-[14px] transition-colors",
    active
      ? "bg-black/[.06] font-medium text-foreground dark:bg-white/[.08]"
      : "text-foreground/80 hover:bg-black/[.04] hover:text-foreground dark:hover:bg-white/[.05]",
  );
  const body = (
    <>
      <item.icon className="size-[17px] shrink-0 opacity-80" />
      {item.label}
    </>
  );
  // Classic pages are outside this app: a full page load, not a client-side route change.
  return item.classic ? (
    <a href={item.href} className={cls} onClick={onNavigate}>
      {body}
    </a>
  ) : (
    <Link href={item.href} className={cls} aria-current={active ? "page" : undefined} onClick={onNavigate}>
      {body}
    </Link>
  );
}

export function Sidebar({ onNavigate }: { onNavigate: () => void }) {
  const pathname = usePathname();
  const stats = usePoll<Stats>("/api/stats?period=today", 30_000).data;
  const auto = usePoll<Auto>("/api/auto", 30_000).data;

  const budget = auto?.settings.budget_rs ?? 0;
  const spent = stats?.cost_rs ?? 0;
  const used = budget ? Math.min(100, Math.round((100 * spent) / budget)) : null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 px-4 pt-4 pb-3">
        <div className="grid size-9 place-items-center rounded-md bg-primary text-primary-foreground">
          <AudioLines className="size-[18px]" />
        </div>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[15px] font-semibold">Vedic Shop</div>
          <div className="truncate text-[12.5px] text-muted-foreground">Voice Agent</div>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 pb-3">
        {NAV.map((group) => (
          <div key={group.title} className="mt-4 first:mt-3">
            <div className="px-2.5 pb-1.5 text-[12.5px] text-muted-foreground">{group.title}</div>
            <div className="flex flex-col gap-0.5">
              {group.items.map((item) => (
                <NavLink key={item.label} item={item} active={!item.classic && pathname === item.href} onNavigate={onNavigate} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="space-y-3 p-2 pb-3">
        <div className="rounded-lg border bg-card p-3">
          <div className="flex items-baseline justify-between text-[12.5px]">
            <span className="text-muted-foreground">Spent today</span>
            <span className="font-medium tabular">{used == null ? "" : `${used}%`}</span>
          </div>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-brand transition-[width]" style={{ width: `${used ?? 0}%` }} />
          </div>
          <div className="mt-2 text-[12.5px] text-muted-foreground tabular">
            {fmtRs(spent)}
            {budget ? ` of ${fmtRs(budget)} daily budget` : ""}
          </div>
        </div>
        <div className="flex items-center gap-2.5 px-2 text-[13px]">
          <span className={cn("size-2 rounded-full", auto?.running ? "bg-good" : "bg-subtle-foreground")} />
          <span className="text-muted-foreground">Auto-calling</span>
          <span className="ml-auto font-medium">{auto == null ? "…" : auto.running ? "On" : "Off"}</span>
        </div>
      </div>
    </div>
  );
}
