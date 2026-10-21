import { CheckCircle2 } from "lucide-react";
import type { AttentionItem } from "@/lib/api";
import { NAV_ITEMS, classic } from "@/lib/nav";
import { cn } from "@/lib/utils";

const DOT: Record<AttentionItem["level"], string> = { critical: "bg-bad", warning: "bg-warn", info: "bg-info" };

const pageLabel = (page: string) => NAV_ITEMS.find((i) => i.href === classic(page))?.label ?? page;

export function AttentionList({ items }: { items: AttentionItem[] }) {
  if (!items.length) {
    return (
      <div className="flex items-center gap-2.5 px-4 py-6 text-muted-foreground">
        <CheckCircle2 className="size-4 text-good" />
        All clear. Nothing needs you right now.
      </div>
    );
  }
  return (
    <ul className="divide-y">
      {items.map((item, i) => (
        <li key={i} className="flex gap-3 px-4 py-3.5">
          <span className={cn("mt-[7px] size-2 shrink-0 rounded-full", DOT[item.level])} />
          <div className="min-w-0">
            <div className="font-medium text-[14px] leading-snug">{item.title}</div>
            <div className="mt-0.5 text-[12.5px] leading-snug break-words text-muted-foreground">{item.detail}</div>
            <a href={classic(item.page)} className="mt-1.5 inline-block text-[12.5px] font-medium hover:underline">
              Open {pageLabel(item.page)} →
            </a>
          </div>
        </li>
      ))}
    </ul>
  );
}
