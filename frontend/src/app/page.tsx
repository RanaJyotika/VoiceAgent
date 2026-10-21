import type { Metadata } from "next";
import { OverviewView } from "@/components/overview/overview-view";

export const metadata: Metadata = { title: "Overview · Voice Agent" };

export default function OverviewPage() {
  return <OverviewView />;
}
