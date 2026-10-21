"use client";

import { useRef, useState } from "react";
import { ChevronDown, Download, FileSpreadsheet, Trash2, Upload, UserPlus } from "lucide-react";
import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { inputClass } from "@/components/ui/dialog";
import { useFeedback } from "@/components/ui/feedback";
import { Select } from "@/components/ui/select";
import { send, type CallType, type CampaignRow, type PreviewIssue, type SheetPreview } from "@/lib/api";
import { cn } from "@/lib/utils";
import { plural } from "./shared";

const ISSUE: Record<PreviewIssue, { label: string; tone: Tone }> = {
  ok: { label: "OK", tone: "good" },
  no_phone: { label: "No valid phone", tone: "bad" },
  duplicate: { label: "Repeated in sheet", tone: "warn" },
  on_list: { label: "Already in this campaign", tone: "warn" },
  do_not_call: { label: "Asked not to be called", tone: "bad" },
};

const COL_LABEL: Record<string, string> = {
  order_id: "Order / booking ID",
  customer_name: "Customer name",
  phone: "Phone",
  product: "Product / puja",
  quantity: "Qty",
  amount: "Amount",
  payment_mode: "Payment",
  address: "Address",
  delivery_date: "Delivery date",
  language: "Language",
  notes: "Notes",
};

type Props = {
  camp: CampaignRow;
  type: CallType | undefined;
  count: number;
  onChanged: () => void;
  /** all: upload and add by hand (the campaign page); upload / manual: one of them (the wizard's Audience step). */
  show?: "all" | "upload" | "manual";
  /** Said under "Upload the Excel sheet" in place of the column list. */
  uploadNote?: React.ReactNode;
};

/** Step 1: the contact list. A sheet is shown first ("Check the sheet") and only added on Import. */
export function ContactsCard({ camp, type, count, onChanged, show = "all", uploadNote }: Props) {
  const { toast, confirm } = useFeedback();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState<SheetPreview | null>(null);
  const [reading, setReading] = useState(false);
  const [filter, setFilter] = useState<"all" | PreviewIssue>("all");
  const [skipDup, setSkipDup] = useState(true);
  const [skipNoPhone, setSkipNoPhone] = useState(false);
  const [importing, setImporting] = useState(false);
  const [addOpen, setAddOpen] = useState(show === "manual");
  const [one, setOne] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState(false);

  const reset = () => {
    setFile(null);
    setPreview(null);
    setFilter("all");
    if (fileInput.current) fileInput.current.value = "";
  };

  const read = async (f: File) => {
    setFile(f);
    setPreview(null);
    setReading(true);
    try {
      const fd = new FormData();
      fd.append("file", f);
      const r = await fetch(`/api/campaigns/${camp.id}/preview`, { method: "POST", body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || (r.status === 404 ? "the server is running older code. Restart run_phone.bat" : `${r.status}`));
      setPreview(j);
    } catch (e) {
      toast(`Could not read the file: ${(e as Error).message}`, true);
      reset();
    } finally {
      setReading(false);
    }
  };

  const doImport = async () => {
    if (!file) return;
    setImporting(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const qs = new URLSearchParams({ skip_duplicates: String(skipDup), skip_no_phone: String(skipNoPhone) });
      const r = await fetch(`/api/campaigns/${camp.id}/upload?${qs}`, { method: "POST", body: fd });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `${r.status}`);
      toast(`Added ${plural(j.imported, "contact")}${j.skipped ? ` · ${j.skipped} skipped` : ""}. Next: start calling (step 2).`);
      reset();
      onChanged();
    } catch (e) {
      toast(`Import failed: ${(e as Error).message}`, true);
    } finally {
      setImporting(false);
    }
  };

  const addOne = async () => {
    setAdding(true);
    try {
      await send(`/api/campaigns/${camp.id}/contacts`, "POST", one);
      toast("Contact added.");
      setOne({});
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setAdding(false);
    }
  };

  const clear = async () => {
    const ok = await confirm({
      title: "Clear this list?",
      body: `Every contact in "${camp.name}" is deleted. Call history keeps its records.`,
      confirmLabel: "Delete all contacts",
      danger: true,
    });
    if (!ok) return;
    try {
      await send(`/api/campaigns/${camp.id}/contacts`, "DELETE");
      toast("List cleared.");
      onChanged();
    } catch (e) {
      toast((e as Error).message, true);
    }
  };

  const c = preview?.counts;
  const willImport = c ? c.ok + (skipDup ? 0 : c.duplicate + c.on_list) + (skipNoPhone ? 0 : c.no_phone) : 0;
  const warns: string[] = [];
  if (preview && c) {
    if (preview.missing.length)
      warns.push(
        `Missing column${preview.missing.length > 1 ? "s" : ""} this kind of call needs: ${preview.missing.join(", ")}. The agent will have nothing to say for ${preview.missing.length > 1 ? "those" : "that"}.`,
      );
    if (c.no_phone) warns.push(`${c.no_phone} row${c.no_phone > 1 ? "s have" : " has"} no valid 10-digit phone.`);
    if (c.duplicate) warns.push(`${c.duplicate} row${c.duplicate > 1 ? "s repeat" : " repeats"} a number used earlier in the sheet.`);
    if (c.on_list) warns.push(`${c.on_list} number${c.on_list > 1 ? "s are" : " is"} already in this campaign.`);
    if (c.do_not_call) warns.push(`${c.do_not_call} number${c.do_not_call > 1 ? "s" : ""} asked not to be called again and will be left out.`);
  }
  const rows = (preview?.rows ?? []).filter((r) => filter === "all" || r.issue === filter);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <span className="text-muted-foreground">{count ? `${count} on the list` : "None yet"}</span>
        <a href={`/api/call-types/${camp.call_type}/template`} className="ml-auto inline-flex items-center gap-1.5 font-medium hover:underline">
          <Download className="size-4" />
          Download template
        </a>
      </div>

      {show !== "manual" && !preview && (
        <label
          onDragOver={(e) => {
            e.preventDefault();
            setOver(true);
          }}
          onDragLeave={() => setOver(false)}
          onDrop={(e) => {
            e.preventDefault();
            setOver(false);
            const f = e.dataTransfer.files[0];
            if (f) read(f);
          }}
          className={cn(
            "flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border-2 border-dashed px-4 py-7 text-center transition-colors",
            over ? "border-brand bg-brand-soft/50" : "hover:bg-muted/40",
          )}
        >
          {reading ? <FileSpreadsheet className="size-6 animate-pulse text-brand" /> : <Upload className="size-6 text-muted-foreground" />}
          <span className="font-medium">{reading ? `Reading ${file?.name}…` : "Upload the Excel sheet"}</span>
          <span className="max-w-[560px] text-[12.5px] text-muted-foreground">
            .xlsx, or drop it here · you will see what is in it before anything is added
            {uploadNote
              ? null
              : type
                ? ` · Columns: ${type.columns.map((col) => col.label + (col.required ? " *" : "")).join(", ")}. Any other column is kept too.`
                : ""}
          </span>
          <input ref={fileInput} type="file" accept=".xlsx,.xlsm" className="sr-only" onChange={(e) => e.target.files?.[0] && read(e.target.files[0])} />
        </label>
      )}
      {show !== "manual" && !preview && uploadNote}

      {preview && c && (
        <div className="rounded-lg border">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b px-4 py-3">
            <div>
              <div className="font-medium">Check the sheet</div>
              <div className="text-[12.5px] text-muted-foreground">
                {file?.name} · {plural(preview.total, "row")} · {willImport} will be added
              </div>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-3 text-[12.5px]">
              <label className="flex cursor-pointer items-center gap-1.5">
                <input type="checkbox" className="accent-[var(--brand)]" checked={skipDup} onChange={(e) => setSkipDup(e.target.checked)} />
                Skip numbers already in this campaign
              </label>
              <label className="flex cursor-pointer items-center gap-1.5">
                <input type="checkbox" className="accent-[var(--brand)]" checked={skipNoPhone} onChange={(e) => setSkipNoPhone(e.target.checked)} />
                Skip rows without a phone
              </label>
              <Button variant="outline" size="sm" onClick={reset} disabled={importing}>
                Cancel
              </Button>
              <Button variant="primary" size="sm" onClick={doImport} disabled={importing || willImport === 0}>
                {importing ? "Importing…" : `Import ${plural(willImport, "contact")}`}
              </Button>
            </div>
          </div>
          <div className="space-y-3 px-4 py-3">
            <div className="flex flex-wrap items-center gap-1.5 text-[12.5px]">
              <span className="text-muted-foreground">Columns read:</span>
              {preview.columns.map((k) => (
                <Badge key={k} tone="good">
                  {COL_LABEL[k] ?? k}
                </Badge>
              ))}
              {preview.extras.map((x) => (
                <Badge key={x.key} tone="good">
                  {x.label}
                </Badge>
              ))}
            </div>
            {warns.length > 0 && <p className="rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">{warns.join(" ")}</p>}
            <div className="flex flex-wrap gap-1.5">
              {(["all", ...(Object.keys(ISSUE) as PreviewIssue[]).filter((k) => c[k])] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  aria-pressed={filter === k}
                  onClick={() => setFilter(k)}
                  className={cn(
                    "rounded-md border px-2.5 py-1 text-[12.5px]",
                    filter === k ? "border-primary bg-primary text-primary-foreground" : "hover:bg-muted",
                  )}
                >
                  {k === "all" ? "All rows" : ISSUE[k].label} <b className="ml-0.5">{k === "all" ? preview.total : c[k]}</b>
                </button>
              ))}
            </div>
          </div>
          <div className="max-h-[50vh] overflow-auto border-t">
            <table className="w-full min-w-[640px] text-left text-[13px]">
              <thead className="sticky top-0 bg-card text-[12px] text-muted-foreground">
                <tr className="border-b">
                  {["Row", "Check", "Customer", "Phone", "Details"].map((h) => (
                    <th key={h} className="px-3 py-2 font-normal">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {rows.map((r) => {
                  const fixed = Object.entries(COL_LABEL)
                    .filter(([k]) => !["customer_name", "phone"].includes(k) && r[k])
                    .map(([k, l]) => `${l}: ${r[k]}`);
                  const extra = Object.values(r.extra ?? {}).map((x) => `${x.label}: ${x.value}`);
                  return (
                    <tr key={r.row}>
                      <td className="px-3 py-2 text-muted-foreground tabular">{r.row}</td>
                      <td className="px-3 py-2">
                        <Badge tone={ISSUE[r.issue]?.tone ?? "neutral"}>{ISSUE[r.issue]?.label ?? r.issue}</Badge>
                      </td>
                      <td className="px-3 py-2 font-medium">{r.customer_name}</td>
                      <td className="px-3 py-2 font-mono text-[12.5px]">{r.phone}</td>
                      <td className="px-3 py-2 text-[12.5px] text-muted-foreground">{[...fixed, ...extra].join(" · ")}</td>
                    </tr>
                  );
                })}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 text-muted-foreground">
                      No rows in this group.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          {preview.total > preview.rows.length && (
            <p className="border-t px-4 py-2 text-[12.5px] text-muted-foreground">
              Showing the first {preview.rows.length} of {preview.total} rows; all of them will be imported.
            </p>
          )}
        </div>
      )}

      <div hidden={show === "upload"}>
        <button
          type="button"
          hidden={show === "manual"}
          onClick={() => setAddOpen(!addOpen)}
          className="flex items-center gap-1 text-[13px] font-medium text-muted-foreground hover:text-foreground"
        >
          <ChevronDown className={cn("size-4 transition-transform", addOpen && "rotate-180")} />
          Add one contact by hand
        </button>
        {addOpen && (
          <div className="mt-3 space-y-3">
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {(type?.columns ?? []).map((col) =>
                col.key === "payment_mode" ? (
                  // Call 509: a payment left blank had the agent guess; the two values the calls understand.
                  <Select tall key={col.key} value={one[col.key] ?? ""} onChange={(e) => setOne({ ...one, [col.key]: e.target.value })} aria-label={col.label}>
                    <option value="">{`${col.label}${col.required ? " *" : ""}`}</option>
                    <option value="COD">COD (cash on delivery)</option>
                    <option value="Prepaid">Prepaid (paid online)</option>
                  </Select>
                ) : (
                <input
                  key={col.key}
                  className={inputClass}
                  placeholder={`${col.label}${col.required ? " *" : ""}`}
                  inputMode={col.key === "phone" ? "numeric" : undefined}
                  value={one[col.key] ?? ""}
                  onChange={(e) => setOne({ ...one, [col.key]: col.key === "phone" ? e.target.value.replace(/[^\d+ ]/g, "") : e.target.value })}
                />
                ),
              )}
              <Select tall value={one.language ?? ""} onChange={(e) => setOne({ ...one, language: e.target.value })} aria-label="Language">
                <option value="">Language: the campaign&apos;s</option>
                <option value="hinglish">Hinglish</option>
                <option value="hindi">Hindi</option>
                <option value="english">English</option>
              </Select>
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" onClick={addOne} disabled={adding}>
                <UserPlus />
                {adding ? "Adding…" : "Add contact"}
              </Button>
              <Button variant="outline" className="ml-auto border-bad/40 text-bad hover:bg-bad-soft" onClick={clear} disabled={!count}>
                <Trash2 />
                Clear this list
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
