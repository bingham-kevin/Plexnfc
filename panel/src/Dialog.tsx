import { useEffect, useRef, type ReactNode } from "react";

/** A modal dialog (the browser's own <dialog>): Escape or the ✕ closes it. */
export function Dialog({ title, onClose, children, wide = false }:
  { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => dialog?.close();
  }, []);
  return (
    <dialog ref={ref} className={`dialog${wide ? " wide" : ""}`} onClose={onClose}
            onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      <div className="dialog-body">
        <div className="dialog-head">
          <h2>{title}</h2>
          <button className="link" aria-label="Close" onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </dialog>
  );
}
