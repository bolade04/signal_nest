import { describe, expect, it } from 'vitest';
import {
  ACTION_CLARIFICATIONS,
  ADDRESS_CLARIFICATIONS,
  READER_CLARIFICATIONS,
  TEXT_REMEDY,
} from '@/lib/jurisdictionClarifications';

/**
 * W21 ADDR (W-6; review d1 C-7): the static copy of the structured business-address categories.
 * The API checks each line against its own vocabulary (`test_business_address_action`); here the
 * copy itself is pinned: the five categories, the text-refusal message as review d1 C-7 words it,
 * and nothing that calls an address or a confirmation verified.
 */
describe('the structured business-address clarifications (W21 ADDR)', () => {
  it('names exactly the five categories', () => {
    expect(Object.keys(ADDRESS_CLARIFICATIONS).sort()).toEqual([
      'business_address_conflict',
      'business_address_incomplete',
      'business_address_locality_unconfirmed',
      'business_address_text_conflict',
      'location_unresolved',
    ]);
  });

  it('words the text refusal as a reading, not a fact (review d1 C-7)', () => {
    expect(ADDRESS_CLARIFICATIONS.business_address_text_conflict).toBe(
      "We read the Business address text as naming a different country, or state or province, from this location's. " +
        "Correct that text on the location's Service area tab, or leave it empty and use the business address fields. " +
        'This cannot be confirmed instead of corrected.',
    );
  });

  it('never calls anything verified, and the refusals say they cannot be confirmed away', () => {
    for (const message of [...Object.values(ADDRESS_CLARIFICATIONS), TEXT_REMEDY]) {
      expect(message).not.toMatch(/verif/i);
      expect(message).not.toMatch(/[{}%]/);
    }
    for (const key of ['business_address_conflict', 'business_address_text_conflict']) {
      expect(ADDRESS_CLARIFICATIONS[key]).toMatch(/This cannot be confirmed instead of corrected\.$/);
    }
    expect(TEXT_REMEDY).toBe(
      ' The business address fields are complete, so you can instead leave the Business address text empty.',
    );
  });
});

/**
 * #313 (the brief capture's clarification contract): the free-text reader's categories, RI-1's
 * among them, and the composed vocabulary the brief panel shows by `error.details.reason`. The
 * API checks every line word for word (`test_business_address_action`); here the keys and the
 * composition are pinned. RI-1's wording is provisional until lane GEO-P4 merges, so it is not
 * pinned here; it lives only in `jurisdictionClarifications.ts`.
 */
describe('the reader categories and the composed vocabulary (#313)', () => {
  it('names exactly the reader’s six categories, RI-1’s territory category among them', () => {
    expect(Object.keys(READER_CLARIFICATIONS).sort()).toEqual([
      'address_direction_ambiguous',
      'address_names_another_place',
      'address_place_unclear',
      'address_route_ambiguous',
      'address_suffix_ambiguous',
      'address_territory_unclear', // RI-1 (lane GEO-P4)
    ]);
  });

  it('shows every structured category as it is and every reader category with the remedy', () => {
    expect(Object.keys(ACTION_CLARIFICATIONS).sort()).toEqual(
      [...Object.keys(ADDRESS_CLARIFICATIONS), ...Object.keys(READER_CLARIFICATIONS)].sort(),
    );
    for (const [category, message] of Object.entries(ADDRESS_CLARIFICATIONS)) {
      expect(ACTION_CLARIFICATIONS[category]).toBe(message);
    }
    for (const [category, message] of Object.entries(READER_CLARIFICATIONS)) {
      expect(ACTION_CLARIFICATIONS[category]).toBe(message + TEXT_REMEDY);
    }
  });

  it('every message is fixed text: one sentence or more, no placeholder, nothing called verified', () => {
    for (const message of Object.values(ACTION_CLARIFICATIONS)) {
      expect(message).toMatch(/\.$/);
      expect(message).not.toMatch(/[{}%]/);
      expect(message).not.toMatch(/verif/i);
    }
  });
});

