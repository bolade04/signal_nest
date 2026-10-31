import type { ScoutRequestOut, ScoutRequestUpdate } from '@/api/types';

/** The Scout edit form's helpers (W16 customer repairs §B, PROPOSED). */
type SourceType = NonNullable<ScoutRequestUpdate['source_types']>[number];

/** Keywords as the API cleans them: trimmed, inner spaces collapsed, empties and repeats dropped. */
export function cleanKeywords(values: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of values) {
    const word = raw.split(/\s+/).filter(Boolean).join(' ');
    const key = word.toLocaleLowerCase();
    if (word && !seen.has(key)) {
      seen.add(key);
      out.push(word);
    }
  }
  return out;
}

export interface ScoutEditForm {
  name: string;
  resolved_market: string;
  keywords: string[];
  source_types: string[];
  notes: string;
}

export const formOf = (r: ScoutRequestOut): ScoutEditForm => ({
  name: r.name,
  resolved_market: r.resolved_market ?? '',
  keywords: [...r.keywords],
  source_types: [...r.source_types],
  notes: r.notes ?? '',
});

const sameList = (a: readonly string[], b: readonly string[]) =>
  a.length === b.length && a.every((v, i) => v === b[i]);

const sameSet = (a: readonly string[], b: readonly string[]) =>
  sameList([...new Set(a)].sort(), [...new Set(b)].sort());

/** Optional text as the API stores an edit of it: trimmed, empty -> null. */
const optionalText = (value: string | null | undefined) => (value ?? '').trim() || null;

/**
 * Only the fields the customer changed, as the API expects them (W16 §B, PROPOSED).
 *
 * Both sides are normalized the same way before they are compared (contract §J.2, review r1 F2):
 * a Scout created with untrimmed text (create keeps today's validation, FU-W16-1) is not
 * rewritten by an edit that did not touch that text, and an untouched form sends nothing.
 */
export function changedFields(original: ScoutRequestOut, form: ScoutEditForm): ScoutRequestUpdate {
  const out: ScoutRequestUpdate = {};
  const name = form.name.trim();
  if (name !== original.name.trim()) out.name = name;
  const market = optionalText(form.resolved_market);
  if (market !== optionalText(original.resolved_market)) out.resolved_market = market;
  const keywords = cleanKeywords(form.keywords);
  if (!sameList(keywords, cleanKeywords(original.keywords))) out.keywords = keywords;
  if (!sameSet(form.source_types, original.source_types)) {
    out.source_types = form.source_types as SourceType[];
  }
  const notes = optionalText(form.notes);
  if (notes !== optionalText(original.notes)) out.notes = notes;
  return out;
}

