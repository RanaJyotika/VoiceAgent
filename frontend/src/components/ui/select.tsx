import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

type Props = React.SelectHTMLAttributes<HTMLSelectElement> & { tall?: boolean };

/** A native select dressed like the rest of the page: keyboard and phone pickers work as usual.
 *  tall: form height (dialogs), otherwise toolbar height. */
export function Select({ className, tall, children, ...props }: Props) {
  return (
    <div className={cn("relative", className)}>
      <select
        className={cn(
          "w-full appearance-none rounded-md border border-input bg-card pr-8 pl-3 text-foreground outline-none hover:bg-muted focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20 disabled:opacity-60",
          tall ? "h-10 text-[14px]" : "h-8 text-[13px]",
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
    </div>
  );
}
