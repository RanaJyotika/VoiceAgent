import type { Metadata } from "next";
import { StoreOrders } from "@/components/store/store-orders";

export const metadata: Metadata = { title: "Store orders · Voice Agent" };

export default function StorePage() {
  return <StoreOrders />;
}
