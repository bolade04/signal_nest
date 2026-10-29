import { within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeAll, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * UI-AUDIT F3 / P5: the geocode note was near-white text on a pale orange tint
 * (`text-warning-foreground` on `bg-warning/10`): 1.06:1 in light mode and 1.18:1 in dark,
 * as measured in the browser. `warning-foreground` is the colour for text on a SOLID warning
 * fill. The fix uses existing tokens only, as the e-mail verification banner does.
 *
 * jsdom applies no CSS, so this computes the WCAG contrast of the classes the note actually
 * renders with, from the design tokens in src/index.css (light `:root` and `.dark`), over
 * the background the note sits on (its own tint composited over the dialog surface).
 */

// The stylesheet's source text (the design tokens). vitest runs with `css: false`, which
// turns every .css import, `?raw` included, into "", so the file itself is read; jsdom
// tests still run in Node. (A literal `new URL('…', import.meta.url)` would be rewritten by
// Vite into a served asset URL, hence the path is derived from this module's own URL.)
let CSS = '';
beforeAll(async () => {
  const fsModule = 'node:fs';
  const { readFileSync } = (await import(/* @vite-ignore */ fsModule)) as {
    readFileSync(path: string, encoding: 'utf8'): string;
  };
  const here = decodeURIComponent(new URL(import.meta.url).pathname);
  CSS = readFileSync(here.replace(/pages\/__tests__\/[^/]+$/, 'index.css'), 'utf8');
});

function tokens(selector: ':root' | '.dark'): Map<string, [number, number, number]> {
  const start = CSS.indexOf(`${selector} {`);
  const block = CSS.slice(start, CSS.indexOf('}', start));
  const map = new Map<string, [number, number, number]>();
  for (const m of block.matchAll(/--([\w-]+):\s*([\d.]+)\s+([\d.]+)%\s+([\d.]+)%;/g)) {
    map.set(m[1]!, [Number(m[2]), Number(m[3]), Number(m[4])]);
  }
  return map;
}

function hslToRgb([h, s, l]: [number, number, number]): [number, number, number] {
  const sat = s / 100;
  const lig = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(lig, 1 - lig);
  const f = (n: number) => lig - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0) * 255, f(8) * 255, f(4) * 255];
}

function luminance([r, g, b]: [number, number, number]): number {
  const c = (v: number) => {
    const x = v / 255;
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * c(r) + 0.7152 * c(g) + 0.0722 * c(b);
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/** `text-<token>` and `bg-<token>[/<alpha>]` colour utilities of one element. */
function colourClasses(element: Element, palette: Map<string, unknown>) {
  let text: string | null = null;
  let bg: { token: string; alpha: number } | null = null;
  for (const cls of Array.from(element.classList)) {
    const t = /^text-([\w-]+)$/.exec(cls);
    if (t && palette.has(t[1]!)) text = t[1]!;
    const b = /^bg-([\w-]+?)(?:\/(\d+))?$/.exec(cls);
    if (b && palette.has(b[1]!)) bg = { token: b[1]!, alpha: b[2] ? Number(b[2]) / 100 : 1 };
  }
  return { text, bg };
}

/** The note's text colour and its composited background, in one theme. */
function noteContrast(note: Element, theme: ':root' | '.dark') {
  const palette = tokens(theme);
  const own = colourClasses(note, palette);
  expect(own.text, 'the note sets its own text colour token').not.toBeNull();
  // Background layers from the note outwards until an opaque one (the dialog surface).
  const layers: { token: string; alpha: number }[] = [];
  for (let el: Element | null = note; el; el = el.parentElement) {
    const { bg } = colourClasses(el, palette);
    if (bg) layers.push(bg);
    if (bg && bg.alpha === 1) break;
  }
  const surface = layers[layers.length - 1];
  expect(surface?.alpha, 'an opaque surface under the note').toBe(1);
  let base = hslToRgb(palette.get(surface!.token) as [number, number, number]);
  for (const layer of layers.slice(0, -1).reverse()) {
    const c = hslToRgb(palette.get(layer.token) as [number, number, number]);
    base = [0, 1, 2].map((i) => c[i]! * layer.alpha + base[i]! * (1 - layer.alpha)) as [number, number, number];
  }
  const fg = hslToRgb(palette.get(own.text!) as [number, number, number]);
  return { text: own.text, layers, ratio: contrast(fg, base) };
}

describe('geocode note contrast (UI-AUDIT P5)', () => {
  it('reads at WCAG AA (>= 4.5:1) in the light and the dark theme', async () => {
    server.use(
      http.post(`*${API_PREFIX}/geocode`, () =>
        HttpResponse.json(
          { error: { code: 'location_unresolvable', message: "We couldn't find that place.", request_id: 'req-1' } },
          { status: 422 },
        ),
      ),
    );
    const screen = renderApp(<App />, { route: '/locations' });
    const add = await screen.findAllByRole('button', { name: /^add location$/i });
    await screen.user.click(add[0]!);
    const dialog = await screen.findByRole('dialog', { name: /add location/i });
    await screen.user.type(within(dialog).getByLabelText(/street address/i), 'Atlantis Nowhere');
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
    const note = await within(dialog).findByRole('status');
    expect(note).toHaveTextContent("We couldn't find that place.");

    expect(tokens(':root').get('warning')).toEqual([32, 90, 44]); // the stylesheet was read
    const light = noteContrast(note, ':root');
    const dark = noteContrast(note, '.dark');
    // The same warning tint over the dialog's card surface, unchanged (no redesign).
    expect(light.layers).toEqual([
      { token: 'warning', alpha: 0.1 },
      { token: 'card', alpha: 1 },
    ]);
    expect(light.ratio, `light: text-${light.text} on warning/10 over card`).toBeGreaterThanOrEqual(4.5);
    expect(dark.ratio, `dark: text-${dark.text} on warning/10 over card`).toBeGreaterThanOrEqual(4.5);
  });
});
