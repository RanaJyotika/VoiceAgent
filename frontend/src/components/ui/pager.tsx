"use client";

import { useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { fmtInt } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Rows on one page of every list (owner, 23 Oct 2026: 25 still meant scrolling). */
export const PAGE_SIZE = 15;

/** One page of a list held in full by the page. Goes back to page 1 whenever `reset` changes (pass the filters). */
export function usePaged<T>(all: T[], size = PAGE_SIZE, reset: string = "") {
  const [page, setPage] = useState(1);
  const [seen, setSeen] = useState(reset);
  if (seen !== reset) {
    setSeen(reset);
    setPage(1);
  }
  const pages = Math.max(1, Math.ceil(all.length / size));
  const at = Math.min(page, pages);
  return { rows: all.slice((at - 1) * size, at * size), page: at, pages, setPage, total: all.length, size };
}

type Props = { page: number; pages: number; onPage: (n: number) => void; total?: number; size?: number };

/** Page numbers under a table (1 … 4 5 6 … 12), with "26–50 of 300" on the left when the total is known.
 *  Turning a page brings the table's top back into view. */
export function Pager({ page, pages, onPage, total, size }: Props) {
  const bar = useRef<HTMLDivElement>(null);
  if (total === 0 || (total == null && pages <= 1)) return null;

  const go = (n: number) => {
    onPage(n);
    const table = bar.current?.parentElement;
    const top = table?.getBoundingClientRect().top ?? 0;
    if (top < 0) window.scrollBy({ top: top - 64 }); // under the top bar
  };

  const nums = [...new Set([1, page - 1, page, page + 1, pages])].filter((n) => n >= 1 && n <= pages).sort((x, y) => x - y);
  const cell = "inline-flex h-8 min-w-8 items-center justify-center rounded-md px-2 text-[13px] tabular";
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const n of nums) {
    if (n - last > 1)
      out.push(
        <span key={`gap${n}`} className="px-1 text-muted-foreground">
          …
        </span>,
      );
    out.push(
      <button
        key={n}
        type="button"
        aria-current={n === page ? "page" : undefined}
        onClick={() => go(n)}
        className={cn(cell, n === page ? "bg-primary text-primary-foreground" : "hover:bg-muted")}
      >
        {n}
      </button>,
    );
    last = n;
  }
  return (
    <div ref={bar} className="flex flex-wrap items-center justify-end gap-1 border-t px-4 py-2.5">
      {total != null && size != null && (
        <span className="mr-auto text-[12.5px] text-muted-foreground tabular">
          {fmtInt((page - 1) * size + 1)}–{fmtInt(Math.min(page * size, total))} of {fmtInt(total)}
        </span>
      )}
      {pages > 1 && (
        <>
          <button
            type="button"
            aria-label="Previous page"
            disabled={page === 1}
            onClick={() => go(page - 1)}
            className={cn(cell, "hover:bg-muted disabled:opacity-40")}
          >
            <ChevronLeft className="size-4" />
          </button>
          {out}
          <button
            type="button"
            aria-label="Next page"
            disabled={page === pages}
            onClick={() => go(page + 1)}
            className={cn(cell, "hover:bg-muted disabled:opacity-40")}
          >
            <ChevronRight className="size-4" />
          </button>
        </>
      )}
    </div>
  );
}
