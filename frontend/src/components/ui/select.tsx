import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/** A native select dressed like the rest of the page: keyboard and phone pickers work as usual. */
export function Select({ className, children, ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <div className={cn("relative", className)}>
      <select
        className="h-8 w-full appearance-none rounded-md border border-input bg-card pr-8 pl-3 text-[13px] text-foreground outline-none hover:bg-muted focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20"
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
    </div>
  );
}
