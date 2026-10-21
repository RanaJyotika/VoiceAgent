"use client";

import { createContext, useCallback, useContext, useMemo, useRef, useState } from "react";
import { CheckCircle2, AlertTriangle } from "lucide-react";
import { Button } from "./button";
import { Dialog } from "./dialog";

type Toast = { id: number; text: string; bad?: boolean };
type ConfirmAsk = { title: string; body?: React.ReactNode; confirmLabel?: string; danger?: boolean };

type Ctx = {
  /** A short note at the bottom of the screen for a few seconds. bad: an error. */
  toast: (text: string, bad?: boolean) => void;
  /** A yes/no question in a dialog; resolves true on yes. */
  confirm: (ask: ConfirmAsk) => Promise<boolean>;
};

const FeedbackCtx = createContext<Ctx>({ toast: () => {}, confirm: async () => false });

export function FeedbackProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [ask, setAsk] = useState<ConfirmAsk | null>(null);
  const answer = useRef<(yes: boolean) => void>(() => {});
  const next = useRef(1);

  const toast = useCallback((text: string, bad = false) => {
    const id = next.current++;
    setToasts((ts) => [...ts, { id, text, bad }]);
    setTimeout(() => setToasts((ts) => ts.filter((t) => t.id !== id)), bad ? 6000 : 3500);
  }, []);

  const confirm = useCallback(
    (a: ConfirmAsk) =>
      new Promise<boolean>((resolve) => {
        answer.current = resolve;
        setAsk(a);
      }),
    [],
  );

  const close = (yes: boolean) => {
    answer.current(yes);
    setAsk(null);
  };

  const value = useMemo(() => ({ toast, confirm }), [toast, confirm]);
  return (
    <FeedbackCtx.Provider value={value}>
      {children}
      <Dialog open={!!ask} onClose={() => close(false)} title={ask?.title ?? ""}>
        {ask?.body && <div className="text-[14px] leading-relaxed text-muted-foreground">{ask.body}</div>}
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="outline" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" className={ask?.danger ? "bg-bad text-white" : undefined} onClick={() => close(true)}>
            {ask?.confirmLabel ?? "OK"}
          </Button>
        </div>
      </Dialog>
      <div className="pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className="pointer-events-auto flex max-w-[520px] items-start gap-2 rounded-lg bg-primary px-4 py-2.5 text-[13.5px] text-primary-foreground shadow-lg"
          >
            {t.bad ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-bad" /> : <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-good" />}
            {t.text}
          </div>
        ))}
      </div>
    </FeedbackCtx.Provider>
  );
}

export const useFeedback = () => useContext(FeedbackCtx);
