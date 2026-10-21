import { CalendarClock, History, LayoutGrid, Megaphone, Phone, ShoppingBag, Ticket, Wallet, type LucideIcon } from "lucide-react";

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

export const NAV: NavGroup[] = [
  {
    title: "Operate",
    items: [
      { label: "Overview", href: "/", icon: LayoutGrid, keywords: "home dashboard" },
      { label: "Store orders", href: classic("store"), icon: ShoppingBag, classic: true, keywords: "shopify orders auto" },
      { label: "Campaigns", href: classic("campaigns"), icon: Megaphone, classic: true, keywords: "contacts upload excel" },
      { label: "Calls", href: classic("history"), icon: History, classic: true, keywords: "history transcripts recordings" },
      { label: "Call back", href: classic("callback"), icon: CalendarClock, classic: true, keywords: "retry later" },
      { label: "Single call", href: classic("call"), icon: Phone, classic: true, keywords: "test browser make call" },
    ],
  },
  {
    title: "Grow",
    items: [{ label: "Coupons", href: classic("coupons"), icon: Ticket, classic: true, keywords: "offers messages whatsapp" }],
  },
  {
    title: "Insights",
    items: [{ label: "Spending", href: classic("spend"), icon: Wallet, classic: true, keywords: "cost billing usage" }],
  },
];

export const NAV_ITEMS = NAV.flatMap((g) => g.items);
