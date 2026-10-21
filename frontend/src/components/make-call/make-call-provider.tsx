"use client";

import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { MakeCallDialog } from "./make-call-dialog";

const Ctx = createContext<{ openMakeCall: () => void }>({ openMakeCall: () => {} });

/** One "Make a call" dialog for the whole app: the Overview button and the search menu both open it. */
export function MakeCallProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const openMakeCall = useCallback(() => setOpen(true), []);
  const value = useMemo(() => ({ openMakeCall }), [openMakeCall]);
  return (
    <Ctx.Provider value={value}>
      {children}
      <MakeCallDialog open={open} onClose={() => setOpen(false)} />
    </Ctx.Provider>
  );
}

export const useMakeCall = () => useContext(Ctx);
