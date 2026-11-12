import { formatDateTime } from '@/lib/utils';

/**
 * W21 ADDR: the Locations dialog's static copy for the structured business address and the
 * customer's locality confirmation. Customer-supplied data is labelled as provided by your team,
 * and a confirmation as confirmed by your team (operator instruction of 2026-11-12, item 3).
 * The API's refusals are shown as the API words them (fixed messages that never repeat what was
 * entered).
 */
export const ADDRESS_HELP =
  'Required for jurisdiction: the business country; in the United States, Canada, Australia and ' +
  'Nigeria, the business state or province; and, when this location has a city, the business town ' +
  "or locality (the town, locality or named place used in the business's postal address). " +
  'Provided by your team.';
export const TOWN_DIFFERS = "The business town or locality is not this location's city.";
export const CONFIRM_LABEL = "Confirm it lies within this location's area";
export const CONFIRM_HELP =
  "Use this when the business's town is part of this location's area, for example a suburb. " +
  "It records your team's own confirmation.";
export const SAVE_FIRST = 'Save your changes first.';
export const WITHDRAW_LABEL = 'Withdraw confirmation';
export function confirmedStatus(at: string | null | undefined): string {
  return (
    `Confirmed by your team on ${formatDateTime(at)}: the business town or locality lies ` +
    "within this location's area."
  );
}

