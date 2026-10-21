"use client";

import { useEffect, useState } from "react";
import { MakeCallProvider } from "@/components/make-call/make-call-provider";
import { FeedbackProvider } from "@/components/ui/feedback";
import { cn } from "@/lib/utils";
import { CommandMenu } from "./command-menu";
import { Sidebar } from "./sidebar";
import { Topbar } from "./topbar";

/** Sidebar | top bar + page. Below lg the sidebar slides in over the page. */
export function AppShell({ children }: { children: React.ReactNode }) {
  const [collapsed, setCollapsed] = useState(false); // desktop
  const [mobileOpen, setMobileOpen] = useState(false); // phone / tablet
  const [searchOpen, setSearchOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearchOpen((o) => !o);
      }
      if (e.key === "Escape") setMobileOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const toggleSidebar = () => {
    if (window.matchMedia("(min-width: 1024px)").matches) setCollapsed((c) => !c);
    else setMobileOpen((o) => !o);
  };

  return (
    <FeedbackProvider>
      <MakeCallProvider>
        <div className="flex min-h-screen">
          <aside
            className={cn(
              "fixed inset-y-0 left-0 z-40 w-[256px] border-r bg-sidebar transition-transform duration-200 lg:sticky lg:top-0 lg:h-screen",
              mobileOpen ? "translate-x-0 shadow-xl" : "-translate-x-full",
              collapsed ? "lg:hidden" : "lg:translate-x-0",
            )}
            aria-label="Sections"
          >
            <Sidebar onNavigate={() => setMobileOpen(false)} />
          </aside>
          {mobileOpen && <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={() => setMobileOpen(false)} aria-hidden />}

          <div className="flex min-w-0 flex-1 flex-col">
            <Topbar onToggleSidebar={toggleSidebar} onOpenSearch={() => setSearchOpen(true)} />
            <main className="min-w-0 flex-1">{children}</main>
          </div>

          <CommandMenu open={searchOpen} onClose={() => setSearchOpen(false)} />
        </div>
      </MakeCallProvider>
    </FeedbackProvider>
  );
}
