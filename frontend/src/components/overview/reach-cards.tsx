import { fmtInt, fmtPct, pct } from "@/lib/format";
import { StatCard } from "./stat-card";

/** sales.reach: customers called or sent a WhatsApp message at least once, and who of them ordered. Test calls and
 * messages are not counted. */
export type Reach = {
  reached: number; // called or messaged
  called: number;
  messaged: number;
  both: number; // called and messaged
  ordered: number; // ordered after: by phone within the window of a call, or from the button's UTMs
  not_bought: number;
  days: number | null;
};

export function ReachCards({ r }: { r: Reach }) {
  const of = (n: number) => (r.reached ? `${fmtPct(pct(n, r.reached))} of customers reached` : "");
  return (
    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <StatCard
        label="Customers reached"
        value={fmtInt(r.reached)}
        note={`Called or messaged at least once · ${fmtInt(r.called)} called · ${fmtInt(r.messaged)} messaged`}
      />
      <StatCard label="Called and messaged" value={fmtInt(r.both)} note={of(r.both) || "Got a call and a WhatsApp message"} />
      <StatCard label="Ordered" value={fmtInt(r.ordered)} note={of(r.ordered) || "Ordered after our call or message"} />
      <StatCard label="Not bought yet" value={fmtInt(r.not_bought)} note={of(r.not_bought) || "Reached, no order yet"} />
    </div>
  );
}
