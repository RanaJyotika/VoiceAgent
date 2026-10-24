"use client";

import { useEffect, useRef, type RefObject } from "react";

/** Widths of the side panels open now; the page keeps the widest one free on the right (AppShell's main reads --side-panel). */
const open = new Map<symbol, number>();
const apply = () => document.documentElement.style.setProperty("--side-panel", `${Math.max(0, ...open.values())}px`);

/** A right-hand panel: the page makes room for it on wide screens instead of hiding under it,
 *  and a click on the page outside it closes it (unless closeOnOutside is false, e.g. while a panel opened from it is up). */
export function useSidePanel(ref: RefObject<HTMLElement | null>, width: number, onClose: () => void, closeOnOutside = true) {
  const close = useRef(onClose);
  useEffect(() => {
    close.current = onClose;
  }, [onClose]);

  useEffect(() => {
    const id = Symbol();
    open.set(id, width);
    apply();
    return () => {
      open.delete(id);
      apply();
    };
  }, [width]);

  useEffect(() => {
    if (!closeOnOutside) return;
    const onClick = (e: MouseEvent) => {
      const t = e.target as Element | null;
      // Gone already (the click re-rendered it), inside the panel, or inside a dialog over the page: not a click on the page.
      if (!t?.isConnected || ref.current?.contains(t) || t.closest("[role=dialog]")) return;
      close.current();
    };
    // Listen from the next tick, so the click that opened the panel does not close it straight away.
    const t = setTimeout(() => document.addEventListener("click", onClick));
    return () => {
      clearTimeout(t);
      document.removeEventListener("click", onClick);
    };
  }, [ref, closeOnOutside]);
}
