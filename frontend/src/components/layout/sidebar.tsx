"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AudioLines, Bot, Phone } from "lucide-react";
import { usePoll } from "@/hooks/use-poll";
import { useConfig } from "@/hooks/use-static";
import type { PhoneLine } from "@/lib/api";
import { fmtPhone } from "@/lib/format";
import { NAV, type NavItem } from "@/lib/nav";
import { cn } from "@/lib/utils";
import { fitVoice, remembered } from "@/lib/voices";

/** The page or one under it: /campaigns/ is lit on /campaigns/view/?id=3 too. */
const isActive = (pathname: string, href: string) => (href === "/" ? pathname === "/" : pathname.replace(/\/$/, "").startsWith(href.replace(/\/$/, "")));

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
  // Owner, 22 Oct 2026: the phone line calls go out on and the agent that speaks, in place of the spend box.
  const line = usePoll<PhoneLine>("/api/plivo/status", 60_000).data;
  const cfg = useConfig();

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
                <NavLink key={item.label} item={item} active={!item.classic && isActive(pathname, item.href)} onNavigate={onNavigate} />
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="p-2 pb-3">
        <div className="space-y-2.5 rounded-lg border bg-card p-3 text-[12.5px]">
          <div className="flex items-start gap-2.5">
            <Phone className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5 text-muted-foreground">
                Phone line
                {line && (
                  <span className={cn("ml-auto inline-flex items-center gap-1 font-medium", line.ready ? "text-good" : "text-warn")}>
                    <span className="size-1.5 rounded-full bg-current" />
                    {line.ready ? "Ready" : "Not ready"}
                  </span>
                )}
              </div>
              <div
                className="mt-0.5 truncate font-mono text-[13px] font-medium"
                title={line?.missing.length ? `Missing: ${line.missing.join(", ")}` : undefined}
              >
                {line == null ? "…" : line.from_number ? fmtPhone(line.from_number) : "No number set"}
              </div>
              {line?.dial_only_my_number && <div className="mt-0.5 text-[11.5px] text-warn">Test mode: rings only your phone</div>}
              {line && !line.ready && !line.dial_only_my_number && (
                <div className="mt-0.5 text-[11.5px] text-muted-foreground">Start with run_phone.bat to call</div>
              )}
            </div>
          </div>
          <div className="flex items-start gap-2.5 border-t pt-2.5">
            <Bot className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <div className="text-muted-foreground">Agent for calls</div>
              <div className="mt-0.5 truncate text-[13px] font-medium">
                {cfg ? `${cfg.model_labels[cfg.model] ?? cfg.model} · ${fitVoice(cfg, cfg.model, remembered("voice") || cfg.voice)}` : "…"}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
