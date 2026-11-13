/**
 * The closed vocabulary of the jurisdiction check before a jurisdiction-dependent action
 * (apps/api/app/jurisdiction/business_address.py `ACTION_CLARIFICATIONS`), mirrored as static
 * copy so the brief panel (#313) shows the category the API names in `error.details.reason`
 * without ever rendering the server's text. The API's test `test_business_address_action`
 * checks each line here, word for word, against its vocabulary:
 *
 * - `ADDRESS_CLARIFICATIONS`: W21 ADDR's structured business-address categories;
 * - `READER_CLARIFICATIONS`: the free-text reader's categories (apps/api/app/jurisdiction/
 *   resolver.py `CLARIFICATIONS`, the geography lane's), each shown with `TEXT_REMEDY`.
 */
export const ADDRESS_CLARIFICATIONS: Readonly<Record<string, string>> = {
  location_unresolved: "This location's country, or its state or province, is missing or not recognised. Open the location and, on its Details tab, enter its country and, for a location in the United States, Canada, Australia or Nigeria, its state or province. Then try again.",
  business_address_incomplete: "This location's business address is incomplete. Open the location and, on its Service area tab, enter the business country; for a business in the United States, Canada, Australia or Nigeria, the business state or province; and, when the location has a city, the business town or locality: the town, locality or named place used in the business's postal address. If the location's own city is wrong, correct it on its Details tab instead. Then try again.",
  business_address_conflict: "The business country, or the business state or province, is not this location's own. Correct the business address on the location's Service area tab, or the location's country or state or province on its Details tab, so that both name the same place. This cannot be confirmed instead of corrected.",
  business_address_text_conflict: "We read the Business address text as naming a different country, or state or province, from this location's. Correct that text on the location's Service area tab, or leave it empty and use the business address fields. This cannot be confirmed instead of corrected.",
  business_address_locality_unconfirmed: "The business town or locality is not this location's city. If the business is in the location's city, enter the same name in both. If the town or locality lies within this location's area, for example a suburb, confirm that on the location's Service area tab. Otherwise correct the business address or the location's city.",
};

/** Appended to each free-text reader category's message once the fields are complete. */
export const TEXT_REMEDY = " The business address fields are complete, so you can instead leave the Business address text empty.";

/**
 * The free-text reader's categories and their fixed messages (resolver.py `CLARIFICATIONS`).
 */
export const READER_CLARIFICATIONS: Readonly<Record<string, string>> = {
  address_suffix_ambiguous: "We couldn't tell whether the end of the business address is a street type or a state or country. Write the street type or direction in full (for example 'Street', 'Court' or 'Southeast'), or add the city and state or country after a comma (for example '1500 Broadway, Hartford, CT').",
  address_names_another_place: "The business address names a state, province or country that is not this location's, as well as the location's own. If that part of the address is the town, write the state, province or region code right after it without a comma (for example 'Victoria BC' or 'Jamaica NY'), or leave the town out. If it is the name of the business or a building, leave it out of the address. Otherwise correct or remove it.",
  address_place_unclear: "We couldn't tell whether a word in the business address is a state, province or country, or the name of a town. If it is the town, write the state, province or region code right after it without a comma (for example 'Washington DC' or 'Victoria BC'), or leave the town out. If it is a state, province or country, write its full name (for example 'Lagos State' or 'Nigeria') after the town, separated by a comma. If it is the name of the business or a building, leave it out of the address. Otherwise correct or remove it.",
  address_route_ambiguous: "We couldn't tell whether a route or unit number in the business address (such as 'SR 520', 'FM 1960' or 'Unit 5 NY 10001') is part of the street or a state or country. After it, write the town and the state or province in full, separated by commas (for example '1 SR 520, Redmond, Washington'), or write the road's name in full (for example '1 State Route 520').",
  address_direction_ambiguous: "We couldn't tell whether a part of the business address that is only a direction (such as 'NE' or 'SE') is the street's direction or a state or country. Write the direction in full after the street name (for example '1200 Pennsylvania Avenue Southeast'), or add the city and state or country after a comma.",
  // RI-1 SLOT (lane GEO-P4): the category the coordinator named on 2026-11-13. Its wording is
  // GEO-P4's provisional message, not final until GEO-P4's review: after GEO-P4 merges, replace
  // the text on the next line with the reader's final message (the API's test then checks it
  // word for word; until then it checks only that the key is here).
  address_territory_unclear: "We couldn't match a territory named in the business address to a present-day state, province, region or country: a historical territory's borders are not a present-day state's. Write the present-day state, province, region or country instead, by its full name or its code (for example 'North Dakota' or 'ND', or 'Lagos State'), or leave that part out of the address.",
};

/**
 * What the brief panel shows for each category: the structured-address messages as they are,
 * and each reader message with `TEXT_REMEDY` (the check asks about the text only once the
 * fields are complete), exactly as the API composes `ACTION_CLARIFICATIONS`.
 */
export const ACTION_CLARIFICATIONS: Readonly<Record<string, string>> = {
  ...ADDRESS_CLARIFICATIONS,
  ...Object.fromEntries(
    Object.entries(READER_CLARIFICATIONS).map(([category, message]) => [category, message + TEXT_REMEDY]),
  ),
};
