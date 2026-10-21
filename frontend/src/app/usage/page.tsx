import type { Metadata } from "next";
import { UsagePage } from "@/components/usage/usage-page";

export const metadata: Metadata = { title: "Usage · Voice Agent" };

export default function Usage() {
  return <UsagePage />;
}
