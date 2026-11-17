"use client";

import { useMemo, useState } from "react";
import { Check, ChevronRight, Edit3, ArrowLeft } from "lucide-react";

import { Modal, useToast } from "@/components/kit/overlays";
import { Badge, Banner, Btn, KV, Mono } from "@/components/kit/primitives";
import { useControl } from "@/lib/api/client";
import type { Json, Setting } from "@/lib/api/types";
import { jsonKind, kindAllowed, lineDiff } from "./diff";

export interface EditRequest { setting: Setting; proposed?: Json }

/**
 * Two steps: edit the JSON value (validated live against the API's rules), then confirm a line diff.
 * `proposed` skips straight to the confirm step (used by the switches).
 */
export function SettingEditModal({ req, onClose }: { req: EditRequest | null; onClose: () => void }) {
  if (!req) return null;
  // keyed so every new request starts from fresh state
  return <EditFlow key={`${req.setting.key}:${JSON.stringify(req.proposed ?? null)}:${req.setting.updated_at}`} req={req} onClose={onClose} />;
}

function EditFlow({ req, onClose }: { req: EditRequest; onClose: () => void }) {
  const toast = useToast();
  const save = useControl<{ key: string; value: Json }, { changed: boolean }>("control/settings");
  const s = req.setting;
  const [text, setText] = useState(() => JSON.stringify(req.proposed !== undefined ? req.proposed : s.value, null, 2));
  const [step, setStep] = useState<"edit" | "confirm">(req.proposed !== undefined ? "confirm" : "edit");

  const parsed = useMemo((): { ok: true; value: Json } | { ok: false; error: string } => {
    try {
      const v = JSON.parse(text) as Json;
      if (!kindAllowed(s.value, v)) return { ok: false, error: `${s.key} is a JSON ${jsonKind(s.value)}; this is a ${jsonKind(v)}.` };
      return { ok: true, value: v };
    } catch (e) {
      return { ok: false, error: `Not valid JSON: ${(e as Error).message}` };
    }
  }, [text, s]);
  const unchanged = parsed.ok && JSON.stringify(parsed.value) === JSON.stringify(s.value);
  const diff = useMemo(() => (parsed.ok ? lineDiff(JSON.stringify(s.value, null, 2), JSON.stringify(parsed.value, null, 2)) : []), [parsed, s]);

  const submit = () => {
    if (!parsed.ok) return;
    save.mutate({ key: s.key, value: parsed.value }, {
      onSuccess: (r) => { toast(r.changed ? `Saved ${s.key}. Applies within 30 s.` : `${s.key} was already that value.`); onClose(); },
    });
  };

  if (step === "edit") {
    return (
      <Modal open onClose={onClose} width={620} title={<>Edit <span className="mono">{s.key}</span></>} icon={<Edit3 size={24} />} iconTone="purple"
        footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="p" disabled={!parsed.ok || unchanged} aria-disabled={!parsed.ok || unchanged} onClick={() => setStep("confirm")}>Review change <ChevronRight size={14} /></Btn></>}>
        <p className="muted" style={{ margin: "6px 0 16px", lineHeight: 1.55 }}>{s.description ?? "No description."}</p>
        <div className="row">
          <label className="flabel" htmlFor="setting-json" style={{ margin: 0 }}>Value · JSON {jsonKind(s.value)}</label>
          <span className="right" aria-live="polite">{parsed.ok ? (unchanged ? <Badge tone="gray">unchanged</Badge> : <Badge tone="green">valid JSON · same type</Badge>) : <Badge tone="red">invalid</Badge>}</span>
        </div>
        <textarea id="setting-json" data-autofocus className="mono-edit mt" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
          aria-invalid={!parsed.ok} aria-describedby="setting-err" rows={Math.min(16, Math.max(4, text.split("\n").length + 1))} />
        {!parsed.ok && <div id="setting-err" className="err-text" role="alert">{parsed.error}</div>}
        <p className="muted xs" style={{ marginTop: 10 }}>Updated {new Date(s.updated_at).toUTCString()}. Clips already approved are not re-checked.</p>
      </Modal>
    );
  }

  return (
    <Modal open onClose={onClose} width={580} title="Save this change?" icon={<Check size={24} strokeWidth={2.4} />} iconTone="purple"
      footer={<>
        {req.proposed === undefined ? <Btn icon={<ArrowLeft size={14} />} onClick={() => setStep("edit")}>Back</Btn> : <Btn onClick={onClose}>Cancel</Btn>}
        <Btn kind="p" disabled={save.isPending || !parsed.ok} onClick={submit}>{save.isPending ? "Saving…" : "Save change"}</Btn>
      </>}>
      <p className="muted" style={{ margin: "6px 0 16px" }}>This environment · <Mono>{s.key}</Mono></p>
      <div className="diff" role="group" aria-label="Changes">
        {diff.map((l, i) => (
          <div key={i} className={l.kind}>
            <span className="sr-only">{l.kind === "del" ? "removed: " : l.kind === "add" ? "added: " : ""}</span>
            {l.kind === "del" ? "- " : l.kind === "add" ? "+ " : "  "}{l.text}
          </div>
        ))}
      </div>
      <div className="mt">
        <KV rows={[["Applies", "within 30 s on every service"], ["Telegram", <span key="t">info · “Setting changed: <span className="mono">{s.key}</span>”</span>],
          ["Audit", "setting.changed event with old and new value"], ["Undo", "edit again — the history keeps every value"]]} />
      </div>
      {save.error && <Banner tone="red" style={{ margin: "14px 0 0" }}><b>{save.error.code === "type_mismatch" ? "Wrong type." : save.error.code === "unknown_setting" ? "Unknown setting." : "Not saved."}</b> {save.error.message}</Banner>}
    </Modal>
  );
}
