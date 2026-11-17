"use client";

import { useId, type ReactNode } from "react";
import { motion } from "motion/react";

/* Small dependency-free SVG charts matching the approved mockups. Every chart has an accessible label. */

type Common = { label: string; width?: number; height?: number };

function Svg({ label, w, h, children }: { label: string; w: number; h: number; children: ReactNode }) {
  return (
    <svg role="img" aria-label={label} viewBox={`0 0 ${w} ${h}`} width="100%" style={{ maxWidth: w, display: "block", overflow: "visible" }} preserveAspectRatio="none">
      <title>{label}</title>
      {children}
    </svg>
  );
}

export function Bars({ values, labels, highlight = -1, color, target, label, width = 270, height = 74 }: Common & {
  values: number[]; labels?: (string | number)[]; highlight?: number | null; color?: string; target?: number;
}) {
  const id = useId().replace(/:/g, "");
  const n = Math.max(values.length, 1);
  const mx = Math.max(...values, target ?? 0, 1);
  const step = width / n;
  const bw = step * 0.62;
  const lh = labels ? 14 : 0;
  const ch = height - lh;
  const hi = highlight === -1 ? n - 1 : highlight;
  return (
    <Svg label={label} w={width} h={height}>
      <defs>
        <linearGradient id={`b1${id}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#C9C2FF" /><stop offset="1" stopColor="#ECE9FF" /></linearGradient>
        <linearGradient id={`b2${id}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#6E5FFF" /><stop offset="1" stopColor="#A69CFF" /></linearGradient>
      </defs>
      {values.map((v, i) => {
        const bh = Math.max(2, (v / mx) * (ch - 4));
        const x = i * step + (step - bw) / 2;
        return (
          <g key={i}>
            <motion.rect x={x} width={bw} rx={Math.min(5, bw / 2)} fill={color ?? (i === hi ? `url(#b2${id})` : `url(#b1${id})`)}
              initial={{ y: ch, height: 0 }} animate={{ y: ch - bh, height: bh }} transition={{ duration: 0.6, delay: i * 0.015, ease: [0.22, 1, 0.36, 1] }}>
              <title>{`${labels?.[i] ?? i}: ${v}`}</title>
            </motion.rect>
            {labels && labels[i] !== "" && <text x={x + bw / 2} y={height - 2} fontSize={9} fill="#6b6b82" textAnchor="middle">{labels[i]}</text>}
          </g>
        );
      })}
      {target !== undefined && (
        <line x1={0} x2={width} y1={ch - (target / mx) * (ch - 4)} y2={ch - (target / mx) * (ch - 4)} stroke="#E5484D" strokeDasharray="4 4" strokeWidth={1.2} />
      )}
    </Svg>
  );
}

export function Line({ values, labels, target, color = "#5A4AF4", dots = true, area = true, markers, lo, label, width = 270, height = 74 }: Common & {
  values: number[]; labels?: string[]; target?: number; color?: string; dots?: boolean; area?: boolean; markers?: { i: number; color: string; title?: string }[]; lo?: number;
}) {
  const id = useId().replace(/:/g, "");
  if (values.length < 2) return <div className="muted xs" style={{ height }}>Not enough data yet</div>;
  const lh = labels ? 14 : 0;
  const ch = height - lh;
  const all = target !== undefined ? [...values, target] : values;
  const mn = lo ?? Math.min(...all);
  const mx = Math.max(...all);
  const rng = mx - mn || 1;
  const n = values.length;
  const pts = values.map((v, i) => [(i / (n - 1)) * (width - 8) + 4, ch - 6 - ((v - mn) / rng) * (ch - 14)] as const);
  const d = "M" + pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" L");
  const every = Math.max(1, Math.floor(n / 8));
  return (
    <Svg label={label} w={width} h={height}>
      <defs>
        <linearGradient id={`ar${id}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={color} stopOpacity={0.22} /><stop offset="1" stopColor={color} stopOpacity={0} /></linearGradient>
      </defs>
      {area && <path d={`${d} L${pts[n - 1][0]},${ch} L${pts[0][0]},${ch} Z`} fill={`url(#ar${id})`} />}
      {target !== undefined && (
        <line x1={0} x2={width} y1={ch - 6 - ((target - mn) / rng) * (ch - 14)} y2={ch - 6 - ((target - mn) / rng) * (ch - 14)} stroke="#E5484D" strokeDasharray="4 4" strokeWidth={1.2} />
      )}
      <motion.path d={d} fill="none" stroke={color} strokeWidth={2.2} strokeLinejoin="round" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.9, ease: "easeOut" }} />
      {dots && pts.map(([x, y], i) => (i % every === 0 || i === n - 1) && <circle key={i} cx={x} cy={y} r={3.2} fill="#fff" stroke={color} strokeWidth={2}><title>{String(values[i])}</title></circle>)}
      {markers?.map((m, k) => pts[m.i] && (
        <g key={k}>
          <line x1={pts[m.i][0]} x2={pts[m.i][0]} y1={4} y2={ch} stroke={m.color} strokeWidth={1.2} strokeDasharray="3 3" />
          <circle cx={pts[m.i][0]} cy={pts[m.i][1]} r={4.5} fill={m.color} stroke="#fff" strokeWidth={2}>{m.title && <title>{m.title}</title>}</circle>
        </g>
      ))}
      {labels?.map((lab, i) => lab && <text key={i} x={(i / (labels.length - 1)) * (width - 8) + 4} y={height - 2} fontSize={9} fill="#6b6b82" textAnchor="middle">{lab}</text>)}
    </Svg>
  );
}

export function Stacked({ series, colors, labels, label, width = 600, height = 160 }: Common & { series: number[][]; colors: string[]; labels?: string[] }) {
  const n = series[0]?.length ?? 0;
  if (!n) return <div className="muted xs">No data</div>;
  const totals = Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + (s[i] ?? 0), 0));
  const mx = Math.max(...totals, 1);
  const step = width / n;
  const bw = step * 0.58;
  const lh = labels ? 16 : 0;
  const ch = height - lh;
  return (
    <Svg label={label} w={width} h={height}>
      {Array.from({ length: n }, (_, i) => {
        let y = ch;
        const x = i * step + (step - bw) / 2;
        return (
          <g key={i}>
            {series.map((s, k) => {
              const bh = ((s[i] ?? 0) / mx) * (ch - 6);
              y -= bh;
              return <motion.rect key={k} x={x} y={y} width={bw} height={bh} fill={colors[k]} initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.02 }} />;
            })}
            {labels?.[i] && <text x={x + bw / 2} y={height - 3} fontSize={9} fill="#6b6b82" textAnchor="middle">{labels[i]}</text>}
          </g>
        );
      })}
    </Svg>
  );
}

export function Ring({ pct, size = 74, color = "#5A4AF4", track = "#EFECFF", text, sub, sw = 8, fs = 15, label }: {
  pct: number; size?: number; color?: string; track?: string; text?: string; sub?: string; sw?: number; fs?: number; label: string;
}) {
  const r = (size - sw) / 2;
  const c = 2 * Math.PI * r;
  const p = Math.max(0, Math.min(100, pct));
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} style={{ flex: "none" }}>
      <title>{label}</title>
      <circle cx={size / 2} cy={size / 2} r={r} stroke={track} strokeWidth={sw} fill="none" />
      <motion.circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth={sw} fill="none" strokeLinecap="round" strokeDasharray={c}
        initial={{ strokeDashoffset: c }} animate={{ strokeDashoffset: c * (1 - p / 100) }} transition={{ duration: 1, ease: [0.22, 1, 0.36, 1] }}
        transform={`rotate(-90 ${size / 2} ${size / 2})`} />
      <text x="50%" y={size / 2 + (sub ? 0 : fs / 3)} textAnchor="middle" fontSize={fs} fontWeight={700} fill="#15151F">{text ?? `${Math.round(p)}%`}</text>
      {sub && <text x="50%" y={size / 2 + 15} textAnchor="middle" fontSize={9} fill="#6b6b82">{sub}</text>}
    </svg>
  );
}

export function Spark({ values, color = "#5A4AF4", width = 90, height = 26, label }: { values: number[]; color?: string; width?: number; height?: number; label: string }) {
  return <Line values={values} width={width} height={height} dots={false} area={false} color={color} label={label} />;
}

export function HBars({ items: all, unit = "", labelWidth = 170 }: { items: { label: ReactNode; value: number; tone?: "red" | "amber" | "green" }[]; unit?: string; labelWidth?: number }) {
  // Values come from free-form JSON: anything that is not a finite number is left out instead of breaking the chart.
  const items = all.filter((i) => typeof i.value === "number" && Number.isFinite(i.value));
  const mx = Math.max(...items.map((i) => i.value), 1);
  if (!items.length) return <div className="muted small">Nothing in this window.</div>;
  return (
    <div>
      {items.map((it, i) => (
        <div className="hb" key={i} style={{ gridTemplateColumns: `${labelWidth}px 1fr 52px` }}>
          <span className="ink2">{it.label}</span>
          <div className={`prog ${it.tone ?? ""}`}><motion.i initial={{ width: 0 }} animate={{ width: `${(it.value / mx) * 100}%` }} transition={{ duration: 0.7, delay: i * 0.05 }} /></div>
          <span className="semi num" style={{ textAlign: "right" }}>{it.value}{unit}</span>
        </div>
      ))}
    </div>
  );
}

export function Legend({ items }: { items: { color: string; label: ReactNode }[] }) {
  return <div className="legend">{items.map((it, i) => <span key={i}><i style={{ background: it.color }} />{it.label}</span>)}</div>;
}

/** Proportional horizontal segments (express stages, render timings). */
export function SegBar({ parts, height = 30 }: { parts: { label: string; value: number; color: string }[]; height?: number }) {
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  return (
    <>
      <div style={{ display: "flex", borderRadius: 12, overflow: "hidden", gap: 2 }} role="img" aria-label={parts.map((p) => `${p.label} ${p.value}s`).join(", ")}>
        {parts.map((p, i) => (
          <motion.div key={i} title={`${p.label} ${p.value}s`} style={{ background: p.color, height }} initial={{ flexGrow: 0 }} animate={{ flexGrow: p.value / total }} transition={{ duration: 0.7, delay: i * 0.05 }} />
        ))}
      </div>
      <div className="mt"><Legend items={parts.map((p) => ({ color: p.color, label: `${p.label} ${p.value}s` }))} /></div>
    </>
  );
}
