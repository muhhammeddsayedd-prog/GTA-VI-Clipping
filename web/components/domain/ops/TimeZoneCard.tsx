"use client";

import { useMemo, useState } from "react";
import { Globe } from "lucide-react";

import { Badge, Btn, Card } from "@/components/kit/primitives";
import { useSecondClock } from "@/components/shell/HeaderClock";
import type { Json, Setting } from "@/lib/api/types";
import { DEFAULT_ZONE, isValidZone, zoneLabel, zoneOffset, zonedParts } from "@/lib/format";

const pad = (n: number) => String(n).padStart(2, "0");

function allZones(): string[] {
  try {
    const list = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? [];
    return ["UTC", ...list.filter((z) => z !== "UTC")];
  } catch {
    return ["UTC", DEFAULT_ZONE];
  }
}

function clockIn(zone: string, now: number): string {
  const p = zonedParts(now, zone);
  return `${pad(p.hour)}:${pad(p.minute)}:${pad(p.second)}`;
}

/** Settings → Time zone: picks `app.timezone`, which every time in the console, Telegram and the daily
 *  report follows. Saving goes through the normal confirm step (`onPropose`). */
export function TimeZoneCard({ setting, onPropose }: { setting: Setting | undefined; onPropose: (s: Setting, v: Json) => void }) {
  const current = typeof setting?.value === "string" ? setting.value : DEFAULT_ZONE;
  const [draft, setDraft] = useState(current);
  const zones = useMemo(() => allZones(), []);
  const now = useSecondClock();
  const valid = isValidZone(draft);
  const changed = valid && draft !== current;

  return (
    <section aria-labelledby="tz-h" id="timezone">
      <div className="flabel" id="tz-h">Time zone</div>
      <Card as="div" style={{ padding: 16 }}>
        <div className="row" style={{ gap: 14, flexWrap: "wrap", alignItems: "flex-start" }}>
          <span className="pic" aria-hidden style={{ flex: "none" }}><Globe size={18} /></span>
          <div style={{ minWidth: 0, flex: "1 1 260px" }}>
            <div className="semi">App time: {zoneLabel(current)} <span className="muted">({current}{now ? ` · ${zoneOffset(current, now)}` : ""})</span></div>
            <div className="mono muted xs" style={{ marginTop: 3 }}>app.timezone</div>
            <div className="muted xs" style={{ marginTop: 8, lineHeight: 1.5 }}>
              Every time in the console and in Telegram, and the day the daily report covers. Limits, caps, budgets and hourly slots stay in UTC; they are labelled UTC wherever they show.
            </div>
          </div>
          {now > 0 && (
            <div className="right" style={{ textAlign: "right" }} aria-live="off">
              <div className="semi num" style={{ fontSize: 20 }}>{clockIn(current, now)}</div>
              <div className="muted xs num">{clockIn("UTC", now)} UTC</div>
            </div>
          )}
        </div>

        {setting ? (
          <div className="row mt" style={{ gap: 10, flexWrap: "wrap", alignItems: "center" }}>
            <label className="field" style={{ flex: "1 1 260px", minWidth: 0 }}>
              <span className="sr-only">Time zone</span>
              <input className="field-input" list="tz-list" value={draft} spellCheck={false} autoComplete="off"
                placeholder="Search a zone, e.g. Europe/London" aria-invalid={!valid}
                onChange={(e) => setDraft(e.target.value.trim())} />
              <datalist id="tz-list">{zones.map((z) => <option key={z} value={z} />)}</datalist>
            </label>
            {valid && draft !== current && now > 0 && <span className="muted xs num">{zoneLabel(draft)} now {clockIn(draft, now)} · {zoneOffset(draft, now)}</span>}
            {!valid && <Badge tone="red">not a time zone</Badge>}
            {current !== DEFAULT_ZONE && <Btn sm onClick={() => onPropose(setting, DEFAULT_ZONE)}>Reset to Cairo</Btn>}
            <Btn sm kind="p" disabled={!changed} aria-disabled={!changed} onClick={() => changed && onPropose(setting, draft)}>Save time zone</Btn>
          </div>
        ) : (
          <div className="muted xs mt">The <span className="mono">app.timezone</span> setting is missing in this environment (migration 0055 not applied); times show in {zoneLabel(DEFAULT_ZONE)}.</div>
        )}
      </Card>
    </section>
  );
}
