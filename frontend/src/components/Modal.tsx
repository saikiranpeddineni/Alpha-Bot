import type { ReactNode } from "react";

export function Modal(props: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  narrow?: boolean;
}) {
  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) props.onClose();
      }}
    >
      <div className={`modal ${props.narrow ? "narrow" : ""}`}>
        <div className="modal-head">
          <span>{props.title}</span>
          <button className="x" onClick={props.onClose} aria-label="Close">✕</button>
        </div>
        {props.children}
      </div>
    </div>
  );
}
