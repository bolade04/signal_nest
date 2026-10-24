import { describe, expect, it } from 'vitest';
import { displayedAt } from '@/test/responsive';

// The helper the header tests rely on: it must read Tailwind's min-width cascade the way
// the generated CSS applies it, and refuse what it cannot read.

function el(html: string): HTMLElement {
  const host = document.createElement('div');
  host.innerHTML = html;
  return host.querySelector('#target')!;
}

describe('displayedAt', () => {
  it('applies the largest matching min-width breakpoint', () => {
    const target = el('<div id="target" class="hidden md:flex xl:hidden"></div>');
    expect([360, 767, 768, 1279, 1280, 1600].map((w) => displayedAt(target, w))).toEqual([
      false,
      false,
      true,
      true,
      false,
      false,
    ]);
  });

  it('hides an element whose ancestor is hidden at that width', () => {
    const target = el('<div class="lg:hidden"><span><b id="target" class="block"></b></span></div>');
    expect(displayedAt(target, 1023)).toBe(true);
    expect(displayedAt(target, 1024)).toBe(false);
  });

  it('treats sr-only as not displayed, and a focus-only reveal as hidden at rest', () => {
    expect(displayedAt(el('<a id="target" class="sr-only focus:not-sr-only"></a>'), 1440)).toBe(false);
    expect(displayedAt(el('<div hidden><i id="target"></i></div>'), 1440)).toBe(false);
  });

  it('ignores utilities that do not set display', () => {
    expect(displayedAt(el('<div id="target" class="min-w-0 truncate md:w-56 [&>span]:truncate"></div>'), 390)).toBe(true);
  });

  it('refuses a display utility under a variant it cannot read', () => {
    expect(() => displayedAt(el('<div id="target" class="max-md:hidden"></div>'), 390)).toThrow(/cannot read/);
    expect(() => displayedAt(el('<div id="target" class="group-hover:block"></div>'), 390)).toThrow(/cannot read/);
  });

  it('refuses two display utilities at one breakpoint', () => {
    expect(() => displayedAt(el('<div id="target" class="flex hidden"></div>'), 390)).toThrow(/one breakpoint/);
  });
});
