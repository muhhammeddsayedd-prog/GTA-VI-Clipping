"use client";

import { useState } from "react";
import { Play, Power, Send } from "lucide-react";

import { Modal, useToast } from "@/components/kit/overlays";
import { Banner, Btn, KV } from "@/components/kit/primitives";
import { useApi, useControl } from "@/lib/api/client";
import type { Guardian } from "@/lib/api/types";
import { fmtTime } from "@/lib/format";

export function StopModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [reason, setReason] = useState("");
  const [typed, setTyped] = useState("");
  const toast = useToast();
  const stop = useControl<{ reason: string }, { changed: boolean }>("control/stop");
  const ready = reason.trim().length >= 3 && typed.trim() === "STOP" && !stop.isPending;
  const submit = () => {
    stop.mutate({ reason: reason.trim() }, {
      onSuccess: () => { toast("Posting stopped. Telegram was told."); setReason(""); setTyped(""); onClose(); },
      onError: (e) => toast(`Could not stop posting: ${e.message}`, true),
    });
  };
  return (
    <Modal open={open} onClose={onClose} title="Stop all posting?" icon={<Power size={26} strokeWidth={2.2} />} iconTone="red"
      footer={<><Btn onClick={onClose}>Cancel</Btn><Btn kind="red" icon={<Power size={14} strokeWidth={2.2} />} disabled={!ready} aria-disabled={!ready} onClick={submit}>{stop.isPending ? "Stopping…" : "Stop posting"}</Btn></>}>
      <p className="muted" style={{ lineHeight: 1.6, margin: "8px 0 18px" }}>
        Nothing will be uploaded to any account until you resume. Discovery, recording, rendering and the quality gate keep running, so the queue keeps filling.
      </p>
      <label className="flabel" htmlFor="stop-reason">Reason <span className="down">*</span></label>
      <textarea id="stop-reason" className="ta" data-autofocus value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} placeholder="Why are you stopping? It is saved with the stop and sent to Telegram." />
      <label className="flabel" htmlFor="stop-typed" style={{ marginTop: 14, display: "block" }}>Type STOP to confirm</label>
      <div className="field"><input id="stop-typed" className="field-input" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" /></div>
      <div className="muted xs" style={{ marginTop: 12, display: "flex", gap: 6 }}><Send size={13} /> Sends “Emergency stop ON” to Telegram and writes a control.stop event.</div>
    </Modal>
  );
}

export function ResumeModal({ open, onClose, reason }: { open: boolean; onClose: () => void; reason: string | null }) {
  const [checked, setChecked] = useState(false);
  const toast = useToast();
  const start = useControl<undefined, { changed: boolean }>("control/start");
  const g = useApi<Guardian>(open ? "admin/guardian" : null, undefined, { interval: false });
  const strikes = g.data?.strikes;
  return (
    <Modal open={open} onClose={onClose} title="Resume posting?" icon={<Play size={22} />} iconTone="green"
      footer={<><Btn onClick={onClose}>Keep stopped</Btn><Btn kind="green" icon={<Play size={12} />} disabled={!checked || start.isPending} aria-disabled={!checked}
        onClick={() => start.mutate(undefined, { onSuccess: () => { toast("Posting resumed."); setChecked(false); onClose(); }, onError: (e) => toast(`Could not resume: ${e.message}`, true) })}>
        {start.isPending ? "Resuming…" : "Resume posting"}</Btn></>}>
      <p className="muted" style={{ lineHeight: 1.6, margin: "8px 0 18px" }}>
        Stop reason: <b className="ink2">{reason ?? "none given"}</b>. A stop never clears itself; only you can resume.
      </p>
      <KV rows={[
        ["Strikes in window", strikes ? <span className={strikes.count >= strikes.limit ? "down" : undefined}>{strikes.count} of {strikes.limit} · {strikes.window_d} days</span> : "…"],
        ["Latest strike", strikes?.items[0] ? `${strikes.items[0].creator?.handle ?? "unknown"} · ${fmtTime(strikes.items[0].at)}` : "—"],
        ["Needs manual delete", g.data ? String(g.data.manual_removals.length) : "…"],
      ]} />
      <Banner tone="amber" style={{ margin: "16px 0 0" }}>After you resume, only strikes newer than this stop count toward the next one.</Banner>
      <label className="row" style={{ marginTop: 16, cursor: "pointer" }}>
        <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} style={{ width: 18, height: 18, accentColor: "var(--p)" }} />
        <span className="ink2">I checked the Guardian page and the manual-removal list</span>
      </label>
    </Modal>
  );
}
