// The jv tokens (jv-tokens.css) read at runtime, for the places CSS can't reach: map paint and inline swatches.
// Nothing here is a colour of its own; every value comes from a token, or a straight mix of two tokens.

export type Ground = "light" | "dark";
export type Tokens = Record<"paper" | "card" | "ink" | "graphite" | "rule" | "cobalt" | "win" | "app", string>;

const cache = new Map<Ground, Tokens>();

/** The tokens for a ground, resolved through a probe element so they don't depend on the page's own theme. */
export function tokensFor(g: Ground): Tokens {
  const hit = cache.get(g);
  if (hit) return hit;
  const probe = document.createElement("div");
  probe.dataset.theme = g;
  probe.style.display = "none";
  document.body.appendChild(probe);
  const cs = getComputedStyle(probe);
  const v = (n: string) => cs.getPropertyValue(n).trim();
  const t: Tokens = { paper: v("--paper"), card: v("--card"), ink: v("--ink"), graphite: v("--graphite"), rule: v("--rule"),
    cobalt: v("--cobalt"), win: v("--win"), app: v("--app-accent") };
  probe.remove();
  if (t.ink) cache.set(g, t); // not resolved yet (stylesheet still loading): don't keep it
  return t;
}

const rgb = (h: string): [number, number, number] => {
  const x = h.replace("#", "");
  return [0, 2, 4].map((i) => parseInt(x.slice(i, i + 2), 16)) as [number, number, number];
};
export function mix(a: string, b: string, t: number): string {
  const [p, q] = [rgb(a), rgb(b)];
  return "#" + p.map((c, i) => Math.round(c + (q[i] - c) * t).toString(16).padStart(2, "0")).join("");
}

/** Road-frequency ramp: rule (driven once) through cobalt to ink (driven most). On the dark ground the
 *  tokens themselves flip (ink is the lightest colour there), so the busiest roads read brightest. */
export function ramp(g: Ground): string[] {
  const t = tokensFor(g);
  return [t.rule, mix(t.rule, t.cobalt, 0.5), t.cobalt, mix(t.cobalt, t.ink, 0.5), t.ink];
}

/** Picked drives and days: the app's one accent, then cobalt, ink and graphite (wraps; routes that share a road are offset). */
export const ROUTE_COLORS = ["var(--app-accent)", "var(--cobalt)", "var(--ink)", "var(--graphite)"];

/** Turn "var(--x)" into a hex for map paint. */
export function resolveColor(c: string, g: Ground): string {
  const m = /^var\(--([\w-]+)\)$/.exec(c);
  if (!m) return c;
  const t = tokensFor(g);
  return ({ "app-accent": t.app, cobalt: t.cobalt, ink: t.ink, graphite: t.graphite, rule: t.rule, win: t.win, paper: t.paper } as Record<string, string>)[m[1]] ?? c;
}
