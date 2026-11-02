import { Badge, type Tone } from "@/components/ui/badge";

/** Which try a call was for its customer in the campaign (owner, 27 Oct 2026): blue the first time the number is
 *  dialled, amber for a retry, red for the last try the campaign allows (or one past it, a call-back). */
export function TryBadge({ n, max }: { n?: number | null; max?: number | null }) {
  if (!n) return null;
  const last = max != null && n >= max;
  const tone: Tone = n === 1 ? "info" : last ? "bad" : "warn";
  const title = `${ordinal(n)} call to this customer in the campaign${max ? ` (the campaign tries ${max} times)` : ""}`;
  return (
    <span title={title} aria-label={title}>
      <Badge tone={tone} className="tabular">
        Try {n}
        {max && n <= max ? `/${max}` : ""}
      </Badge>
    </span>
  );
}

const ordinal = (n: number) => `${n}${n % 100 >= 11 && n % 100 <= 13 ? "th" : ["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
