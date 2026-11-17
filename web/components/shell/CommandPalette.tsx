"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useRouter } from "next/navigation";
import { AnimatePresence, motion } from "motion/react";
import { Bolt, Film, Search, Send, SlidersHorizontal, User, Video, type LucideIcon } from "lucide-react";

import { apiGet } from "@/lib/api/client";
import type { SearchResult } from "@/lib/api/types";
import { NAV } from "./nav";

const KIND_ICON: Record<string, LucideIcon> = { clip: Film, post: Send, creator: User, spike: Bolt, video: Video, setting: SlidersHorizontal };
const PAGES = NAV.flatMap((g) => (g.items ?? [{ href: g.href!, label: g.label, icon: g.icon }]).map((i) => ({ ...i })));

interface Row { key: string; group: string; label: string; sub: string; href: string; icon: LucideIcon }

export function CommandPalette({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (typeof document === "undefined") return null;
  return createPortal(<AnimatePresence>{open && <PaletteBody onClose={onClose} />}</AnimatePresence>, document.body);
}

/** Mounted only while open, so every opening starts with an empty search. */
function PaletteBody({ onClose }: { onClose: () => void }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [found, setFound] = useState<{ term: string; results: SearchResult[] }>({ term: "", results: [] });
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const term = q.trim();
  const results = useMemo(() => (term && found.term === term ? found.results : []), [term, found]);
  const loading = Boolean(term) && found.term !== term;

  useEffect(() => { const t = setTimeout(() => inputRef.current?.focus(), 30); return () => clearTimeout(t); }, []);

  useEffect(() => {
    if (!term) return;
    const ctl = new AbortController();
    const t = setTimeout(() => {
      apiGet<{ results: SearchResult[] }>("admin/search", { q: term }, ctl.signal)
        .then((r) => setFound({ term, results: r.results }))
        .catch(() => { if (!ctl.signal.aborted) setFound({ term, results: [] }); });
    }, 180);
    return () => { clearTimeout(t); ctl.abort(); };
  }, [term]);

  const rows: Row[] = useMemo(() => {
    const lower = term.toLowerCase();
    const hits = results.map((r) => ({ key: `${r.kind}:${r.id}`, group: `${r.kind[0].toUpperCase()}${r.kind.slice(1)}s`, label: r.label, sub: r.sub ?? "", href: r.href, icon: KIND_ICON[r.kind] ?? Search }));
    const pages = PAGES.filter((p) => !lower || p.label.toLowerCase().includes(lower)).map((p) => ({ key: `page:${p.href}`, group: "Pages", label: p.label, sub: "Go to page", href: p.href, icon: p.icon }));
    return [...hits, ...pages].slice(0, 14);
  }, [results, term]);

  const go = (r: Row | undefined) => { if (r) { onClose(); router.push(r.href); } };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, rows.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); go(rows[active]); }
    else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  };

  let lastGroup = "";
  return (
        <>
          <motion.div className="overlay fixed" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-hidden />
          <motion.div role="dialog" aria-modal="true" aria-label="Search the console" className="pop fixed" onKeyDown={onKey}
            style={{ left: "50%", top: 110, width: "min(640px, calc(100vw - 24px))", padding: 16, x: "-50%" }}
            initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.16 }}>
            <div className="field focus" style={{ padding: "14px 16px", fontSize: 15 }}>
              <Search size={18} />
              <input ref={inputRef} className="field-input" value={q} onChange={(e) => { setQ(e.target.value); setActive(0); }}
                placeholder="Search ids, #series, handles, post URLs, setting keys" aria-label="Search" role="combobox" aria-expanded aria-controls="cmd-list"
                aria-activedescendant={rows[active] ? `cmd-${active}` : undefined} style={{ fontSize: 15 }} />
              <span className="kbd">Esc</span>
            </div>
            <div className="muted xs" style={{ margin: "12px 4px 4px" }} aria-live="polite">{loading ? "Searching…" : q && !results.length ? "No matching items — pages below" : "Jump to a page or paste anything"}</div>
            <ul id="cmd-list" role="listbox" style={{ listStyle: "none", margin: 0, padding: 0, maxHeight: "55vh", overflowY: "auto" }}>
              {rows.map((r, i) => {
                const head = r.group !== lastGroup ? <li className="ph" role="presentation" key={`h-${r.group}`}>{r.group}</li> : null;
                lastGroup = r.group;
                const Icon = r.icon;
                return [head, (
                  <li key={r.key} id={`cmd-${i}`} role="option" aria-selected={i === active} className={`pi link${i === active ? " on" : ""}`}
                    onMouseEnter={() => setActive(i)} onClick={() => go(r)}>
                    <span className="pic"><Icon size={16} /></span>
                    <div style={{ minWidth: 0 }}><div className="semi ellipsis">{r.label}</div><div className="t2">{r.sub}</div></div>
                    {i === active && <span className="kbd" style={{ marginLeft: "auto" }}>Enter</span>}
                  </li>
                )];
              })}
            </ul>
            <div className="row muted xs" style={{ marginTop: 10, borderTop: "1px solid var(--line)", paddingTop: 12, gap: 14 }}>
              <span><span className="kbd">↑↓</span> move</span><span><span className="kbd">Enter</span> open</span><span><span className="kbd">Ctrl K</span> toggle</span>
            </div>
          </motion.div>
        </>
  );
}
