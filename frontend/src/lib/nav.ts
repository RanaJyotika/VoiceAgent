import { CalendarClock, History, LayoutGrid, Megaphone, ShoppingBag, Wallet, type LucideIcon } from "lucide-react";

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** Still on the original dashboard (/classic#page) until it is rebuilt here. */
  classic?: boolean;
  keywords?: string;
};

export type NavGroup = { title: string; items: NavItem[] };

/** Where a page of the original dashboard lives. */
export const classic = (page: string) => `/classic#${page}`;

/** Pages rebuilt here, by the name the server and the original dashboard use for them. */
const REBUILT: Record<string, string> = { history: "/calls/", callback: "/callback/", campaigns: "/campaigns/", spend: "/usage/", store: "/store/" };

/** Where a page lives now: here once rebuilt, else on the original dashboard. */
export const pageHref = (page: string) => REBUILT[page] ?? classic(page);

export const NAV: NavGroup[] = [
  {
    title: "Operate",
    items: [
      { label: "Overview", href: "/", icon: LayoutGrid, keywords: "home dashboard" },
      { label: "Store orders", href: "/store/", icon: ShoppingBag, keywords: "shopify orders auto automatic calling batches" },
      { label: "Campaigns", href: "/campaigns/", icon: Megaphone, keywords: "contacts upload excel new campaign" },
      { label: "Calls summary", href: "/calls/", icon: History, keywords: "calls history transcripts recordings" },
      { label: "Call back", href: "/callback/", icon: CalendarClock, keywords: "retry later no answer busy" },
    ],
  },
  {
    title: "Insights",
    items: [{ label: "Usage", href: "/usage/", icon: Wallet, keywords: "spending cost billing budget limits" }],
  },
];

export const NAV_ITEMS = NAV.flatMap((g) => g.items);
