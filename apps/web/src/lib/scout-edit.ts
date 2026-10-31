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

/** Only the fields the customer changed, as the API expects them (W16 §B, PROPOSED). */
export function changedFields(original: ScoutRequestOut, form: ScoutEditForm): ScoutRequestUpdate {
  const out: ScoutRequestUpdate = {};
  const name = form.name.trim();
  if (name !== original.name) out.name = name;
  const market = form.resolved_market.trim() || null;
  if (market !== (original.resolved_market ?? null)) out.resolved_market = market;
  const keywords = cleanKeywords(form.keywords);
  if (!sameList(keywords, original.keywords)) out.keywords = keywords;
  if (!sameList(form.source_types, original.source_types)) {
    out.source_types = form.source_types as SourceType[];
  }
  const notes = form.notes.trim() || null;
  if (notes !== (original.notes ?? null)) out.notes = notes;
  return out;
}

