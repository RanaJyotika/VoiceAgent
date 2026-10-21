import { Suspense } from "react";
import type { Metadata } from "next";
import { CampaignForm } from "@/components/campaigns/campaign-form";

export const metadata: Metadata = { title: "New campaign · Voice Agent" };

// ?edit=<id> changes an existing campaign. Reading the query needs a Suspense boundary in a static build.
export default function NewCampaignPage() {
  return (
    <Suspense>
      <CampaignForm />
    </Suspense>
  );
}
