import type { Metadata } from "next";
import { DraftOrders } from "@/components/drafts/draft-orders";

export const metadata: Metadata = { title: "Draft orders · Voice Agent" };

export default function DraftOrdersPage() {
  return <DraftOrders />;
}
