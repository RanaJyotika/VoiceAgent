import type { Metadata } from "next";
import { CampaignList } from "@/components/campaigns/campaign-list";

export const metadata: Metadata = { title: "Campaigns · Voice Agent" };

export default function CampaignsPage() {
  return <CampaignList />;
}
