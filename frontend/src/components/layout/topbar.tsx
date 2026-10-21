"use client";

import { useSyncExternalStore } from "react";
import { Moon, PanelLeft, Search, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTheme } from "@/hooks/use-theme";
import { Notifications } from "./notifications";

const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform);

export function Topbar({ onToggleSidebar, onOpenSearch }: { onToggleSidebar: () => void; onOpenSearch: () => void }) {
  const { dark, toggle } = useTheme();
  const mac = useSyncExternalStore(
    () => () => {},
    isMac,
    () => false,
  );

  return (
    <header className="sticky top-0 z-20 flex h-[52px] items-center gap-3 border-b bg-background/85 px-3 backdrop-blur sm:px-4">
      <Button variant="ghost" size="icon" onClick={onToggleSidebar} aria-label="Show or hide the menu">
        <PanelLeft />
      </Button>
      <button
        type="button"
        onClick={onOpenSearch}
        className="flex h-9 w-full max-w-[384px] items-center gap-2 rounded-md border border-input bg-card px-3 text-[13.5px] text-muted-foreground hover:bg-muted"
      >
        <Search className="size-4" />
        <span className="flex-1 text-left">Search or jump to…</span>
        <kbd className="hidden rounded border px-1.5 py-px font-sans text-[11px] sm:inline">{mac ? "⌘K" : "Ctrl K"}</kbd>
      </button>
      <div className="ml-auto flex items-center gap-1">
        <Notifications />
        <Button variant="ghost" size="icon" onClick={toggle} aria-label={dark ? "Light mode" : "Dark mode"}>
          {dark ? <Sun /> : <Moon />}
        </Button>
      </div>
    </header>
  );
}
