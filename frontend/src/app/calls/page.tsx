import type { Metadata } from "next";
import { CallsSummary } from "@/components/calls/calls-summary";

export const metadata: Metadata = { title: "Calls summary · Voice Agent" };

export default function CallsPage() {
  return <CallsSummary />;
}
