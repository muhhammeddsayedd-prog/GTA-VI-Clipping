"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { AnimatePresence, motion } from "motion/react";
import { CheckCircle2, X, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import { useMounted } from "@/lib/use-now";

/* Accessible dialog primitives: portal, focus trap, Esc to close, focus restored on close. */

function useFocusTrap(open: boolean, onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const el = ref.current;
    const focusables = () => Array.from(el?.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),textarea,input,select,[tabindex]:not([tabindex="-1"])') ?? []);
    const first = el?.querySelector<HTMLElement>("[data-autofocus]") ?? focusables()[0];
    first?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); return; }
      if (e.key !== "Tab") return;
      const f = focusables();
      if (!f.length) return;
      const a = f[0], z = f[f.length - 1];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = overflow; prev?.focus?.(); };
  }, [open, onClose]);
  return ref;
}

function Portal({ children }: { children: ReactNode }) {
  const mounted = useMounted();
  return mounted ? createPortal(children, document.body) : null;
}

export function Modal({ open, onClose, title, width = 540, icon, iconTone = "purple", children, footer, labelledBy }: {
  open: boolean; onClose: () => void; title: ReactNode; width?: number; icon?: ReactNode; iconTone?: "purple" | "red" | "green" | "amber";
  children: ReactNode; footer?: ReactNode; labelledBy?: string;
}) {
  const ref = useFocusTrap(open, onClose);
  const id = labelledBy ?? "modal-title";
  const tone = { purple: ["var(--ps)", "var(--p)"], red: ["var(--rs)", "var(--red)"], green: ["var(--gs)", "var(--green)"], amber: ["var(--as)", "var(--amber)"] }[iconTone];
  return (
    <Portal>
      <AnimatePresence>
        {open && (
          <>
            <motion.div className="overlay fixed" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-hidden />
            <motion.div ref={ref} role="dialog" aria-modal="true" aria-labelledby={id} className="modal fixed" style={{ width, top: 110, x: "-50%" }}
              initial={{ opacity: 0, y: 18, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 12, scale: 0.98 }} transition={{ type: "spring", damping: 26, stiffness: 320 }}>
              <button type="button" className="ibtn sm x" aria-label="Close" onClick={onClose}><X size={15} /></button>
              {icon && <div className="mi" style={{ background: tone[0], color: tone[1] }}>{icon}</div>}
              <h2 id={id}>{title}</h2>
              {children}
              {footer && <div className="foot">{footer}</div>}
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </Portal>
  );
}

export function Drawer({ open, onClose, title, sub, width = 520, children }: { open: boolean; onClose: () => void; title: ReactNode; sub?: ReactNode; width?: number; children: ReactNode }) {
  const ref = useFocusTrap(open, onClose);
  return (
    <Portal>
      <AnimatePresence>
        {open && (
          <>
            <motion.div className="overlay fixed" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} aria-hidden />
            <motion.aside ref={ref} role="dialog" aria-modal="true" aria-labelledby="drawer-title" className="drawer fixed" style={{ width }}
              initial={{ x: 60, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 60, opacity: 0 }} transition={{ type: "spring", damping: 30, stiffness: 300 }}>
              <div className="row">
                <button type="button" className="back" aria-label="Close" onClick={onClose}><X size={16} /></button>
                <div style={{ minWidth: 0 }}><h2 id="drawer-title">{title}</h2>{sub && <div className="muted small">{sub}</div>}</div>
              </div>
              <div className="mt2">{children}</div>
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </Portal>
  );
}

/** Anchored popover (menus, alert list). Closes on outside click and Esc. */
export function Popover({ open, onClose, children, className, style, label }: { open: boolean; onClose: () => void; children: ReactNode; className?: string; style?: React.CSSProperties; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node) && !(e.target as HTMLElement).closest("[data-pop-trigger]")) onClose(); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [open, onClose]);
  return (
    <AnimatePresence>
      {open && (
        <motion.div ref={ref} role="dialog" aria-label={label} className={cn("pop", className)} style={style}
          initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.16 }}>
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/* ---------- toasts ---------- */
type Toast = { id: number; text: string; err?: boolean };
const ToastCtx = createContext<(text: string, err?: boolean) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, err?: boolean) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, err }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-wrap" role="status" aria-live="polite">
        <AnimatePresence>
          {toasts.map((t) => (
            <motion.div key={t.id} className={cn("toast", t.err && "err")} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }}>
              {t.err ? <XCircle size={16} /> : <CheckCircle2 size={16} />}{t.text}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  );
}
