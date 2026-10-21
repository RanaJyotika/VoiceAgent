"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { CampaignView } from "@/components/campaigns/campaign-view";

// One campaign: /campaigns/view/?id=<id>. A query, not /campaigns/<id>: the static build cannot know the ids.
function ById() {
  const id = useSearchParams().get("id") ?? "";
  return <CampaignView key={id} />; // a fresh view per campaign, so one campaign's form never carries over
}

export default function CampaignPage() {
  return (
    <Suspense>
      <ById />
    </Suspense>
  );
}
