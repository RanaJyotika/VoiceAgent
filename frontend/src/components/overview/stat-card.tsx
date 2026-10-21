import { ArrowDownRight, ArrowUpRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { cn } from "@/lib/utils";

type Props = {
  label: string;
  value: string;
  /** Percent change against the period before; null = nothing to compare with. */
  change?: number | null;
  /** True when a rise is bad news (spend, failures): red up, green down. */
  upIsBad?: boolean;
  note?: string;
};

export function StatCard({ label, value, change, upIsBad, note }: Props) {
  const up = (change ?? 0) >= 0;
  const good = upIsBad ? !up : up;
  return (
    <Card className="px-4 py-3.5">
      <div className="text-[13px] text-muted-foreground">{label}</div>
      <div className="mt-1.5 text-[26px] leading-none font-semibold tracking-tight tabular">{value}</div>
      <div className="mt-2.5 flex items-start gap-1.5 text-[12.5px] leading-snug">
        {change != null && (
          <span className={cn("inline-flex shrink-0 items-center gap-0.5 font-medium tabular", good ? "text-good" : "text-bad")}>
            {up ? <ArrowUpRight className="size-3.5" /> : <ArrowDownRight className="size-3.5" />}
            {Math.abs(change).toFixed(1)}%
          </span>
        )}
        {note && <span className="text-muted-foreground">{note}</span>}
      </div>
    </Card>
  );
}

/** Percent change from `before` to `now`; null when there is nothing before to compare with. */
export const change = (now: number, before: number) => (before ? (100 * (now - before)) / before : null);
