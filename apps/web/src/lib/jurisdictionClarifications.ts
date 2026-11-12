/**
 * W21 ADDR: the structured business-address categories of the jurisdiction check before a
 * jurisdiction-dependent action (apps/api/app/jurisdiction/business_address.py
 * `ADDRESS_CLARIFICATIONS`), mirrored as static copy so the brief panel (#313) can show the
 * category the API names in `error.details.reason` without ever rendering the server's text.
 * The API's test `test_business_address_action` checks each line here against its vocabulary.
 * The free-text reader's own categories (geography lane) are mapped by the brief panel itself.
 */
export const ADDRESS_CLARIFICATIONS: Readonly<Record<string, string>> = {
  location_unresolved: "This location's country, or its state or province, is missing or not recognised. Open the location and, on its Details tab, enter its country and, for a location in the United States, Canada, Australia or Nigeria, its state or province. Then try again.",
  business_address_incomplete: "This location's business address is incomplete. Open the location and, on its Service area tab, enter the business country and, for a business in the United States, Canada, Australia or Nigeria, the business state or province. Then try again.",
  business_address_conflict: "The business country, or the business state or province, is not this location's own. Correct the business address on the location's Service area tab, or the location's country or state or province on its Details tab, so that both name the same place. This cannot be confirmed instead of corrected.",
  business_address_text_conflict: "We read the Business address text as naming a different country, or state or province, from this location's. Correct that text on the location's Service area tab, or leave it empty and use the business address fields. This cannot be confirmed instead of corrected.",
  business_address_locality_unconfirmed: "The business town or locality is not this location's city. If the business is in the location's city, enter the same name in both. If the town or locality lies within this location's area, for example a suburb, confirm that on the location's Service area tab. Otherwise correct the business address or the location's city.",
};

/** Appended to each free-text reader category's message once the fields are complete. */
export const TEXT_REMEDY = " The business address fields are complete, so you can instead leave the Business address text empty.";
