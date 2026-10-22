/**
 * Whether Tailwind's display utilities show an element at a viewport width — jsdom
 * applies no CSS, so a component that hides or shows controls per breakpoint cannot
 * be tested through the layout. This reads the classes instead, for the element and
 * every ancestor, with Tailwind's default screens (tailwind.config.ts sets none).
 *
 * Deliberately narrow: it understands only unprefixed and min-width-prefixed display
 * utilities plus `sr-only`. A display utility under any other variant (`max-md:`,
 * `focus:`, `group-hover:` …), or two display utilities at one breakpoint, throws —
 * a pattern it cannot read is an error, never a guess. It says nothing about size,
 * overflow or clipping: those need a rendered browser.
 */

const SCREENS: Record<string, number> = { sm: 640, md: 768, lg: 1024, xl: 1280, '2xl': 1536 };

const DISPLAY = new Set([
  'hidden',
  'block',
  'inline',
  'inline-block',
  'flex',
  'inline-flex',
  'grid',
  'inline-grid',
  'contents',
  'table',
  'flow-root',
  'list-item',
]);

/** Shown at this width by its own classes (null when they say nothing about display). */
function ownDisplay(element: Element, width: number): boolean | null {
  let shown: boolean | null = null;
  let applied = -1;
  const perScreen = new Map<number, string>();
  for (const cls of Array.from(element.classList)) {
    const at = cls.lastIndexOf(':');
    const variant = at === -1 ? '' : cls.slice(0, at);
    const utility = at === -1 ? cls : cls.slice(at + 1);
    if (!DISPLAY.has(utility) && utility !== 'sr-only' && utility !== 'not-sr-only') continue;
    if (variant !== '' && !(variant in SCREENS)) {
      if (utility === 'not-sr-only') continue; // focus:not-sr-only: a focus-only reveal, hidden at rest
      throw new Error(`displayedAt: cannot read "${cls}" on <${element.tagName.toLowerCase()}>`);
    }
    const min = variant === '' ? 0 : SCREENS[variant]!;
    const clash = perScreen.get(min);
    if (clash) throw new Error(`displayedAt: "${clash}" and "${cls}" both set display at one breakpoint`);
    perScreen.set(min, cls);
    if (width >= min && min > applied) {
      applied = min;
      shown = utility !== 'hidden' && utility !== 'sr-only';
    }
  }
  return shown;
}

/** True when neither the element nor any ancestor is hidden at `width` (CSS px). */
export function displayedAt(element: Element, width: number): boolean {
  for (let el: Element | null = element; el; el = el.parentElement) {
    if (el instanceof HTMLElement && el.hidden) return false;
    if (ownDisplay(el, width) === false) return false;
  }
  return true;
}

/** The elements of `elements` shown at `width`. */
export function shownAt<T extends Element>(elements: readonly T[], width: number): T[] {
  return elements.filter((el) => displayedAt(el, width));
}
