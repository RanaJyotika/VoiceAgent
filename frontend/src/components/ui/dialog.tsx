"use client";

import { useEffect, useRef } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

type Props = {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  className?: string;
  children: React.ReactNode;
};

/** A centred modal: Esc or a click outside closes it, the page behind does not scroll. */
export function Dialog({ open, onClose, title, description, className, children }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  useEffect(() => {
    closeRef.current = onClose;
  });

  // Only on open/close: re-running on every render would pull the focus back to the first field while typing.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeRef.current();
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    box.current?.querySelector<HTMLElement>("input, select, textarea, button:not([data-close])")?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [open]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4 sm:items-center" onMouseDown={onClose}>
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cn("relative my-auto w-full max-w-[460px] rounded-xl border bg-card p-6 shadow-2xl", className)}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          data-close
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 rounded-md p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <X className="size-4" />
        </button>
        <h2 className="pr-8 text-[18px] font-semibold tracking-tight">{title}</h2>
        {description && <p className="mt-1 text-[13.5px] text-muted-foreground">{description}</p>}
        <div className="mt-5">{children}</div>
      </div>
    </div>
  );
}

type FieldProps = { label: string; hint?: React.ReactNode; required?: boolean; optional?: boolean; children: React.ReactNode };

/** A labelled form row, as in the dialog's fields. required: a red star; optional: a grey "optional". */
export function Field({ label, hint, required, optional, children }: FieldProps) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[13.5px] font-medium">
        {label}
        {required && (
          <span className="ml-0.5 text-bad" aria-hidden>
            *
          </span>
        )}
        {optional && <span className="ml-1.5 text-[12px] font-normal text-muted-foreground">optional</span>}
      </span>
      {children}
      {hint && <span className="mt-1 block text-[12px] text-muted-foreground">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "h-10 w-full rounded-md border border-input bg-card px-3 text-[14px] outline-none placeholder:text-subtle-foreground focus-visible:border-brand focus-visible:ring-2 focus-visible:ring-brand/20 disabled:opacity-60";
