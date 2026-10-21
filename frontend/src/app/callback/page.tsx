import type { Metadata } from "next";
import { CallbackList } from "@/components/calls/callback-list";

export const metadata: Metadata = { title: "Call back · Voice Agent" };

export default function CallbackPage() {
  return <CallbackList />;
}
