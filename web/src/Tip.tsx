import { useState, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Hover tooltip with exact values, shown at once (a native title waits ~1 s and can't be styled).
 * Rendered into <body>: the panels use backdrop-filter, which would trap a fixed-position child. */
export function useTip(): [ReactNode, (text: string) => { onMouseMove: (e: MouseEvent) => void; onMouseLeave: () => void }] {
  const [tip, setTip] = useState<{ x: number; y: number; text: string } | null>(null);
  const bind = (text: string) => ({
    onMouseMove: (e: MouseEvent) => setTip({ x: e.clientX, y: e.clientY, text }),
    onMouseLeave: () => setTip(null),
  });
  const flip = tip && tip.x > window.innerWidth - 220;
  const el = tip
    ? createPortal(
        <div className="tip" role="tooltip" style={{ top: tip.y + 14, left: flip ? undefined : tip.x + 12, right: flip ? window.innerWidth - tip.x + 12 : undefined }}>
          {tip.text}
        </div>,
        document.body,
      )
    : null;
  return [el, bind];
}

/** "0", "1.2k", "35k": short labels for a value axis. */
export const short = (v: number) => (v >= 10_000 ? `${Math.round(v / 1000)}k` : v >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${Math.round(v)}`);

/** The smallest 1 / 2 / 2.5 / 5 x 10^k at or above v: the top of a value axis, so ticks are round numbers. */
export function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return ([1, 2, 2.5, 5, 10].find((m) => m * p >= v) ?? 10) * p;
}
