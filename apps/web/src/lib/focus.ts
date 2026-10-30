/**
 * Move keyboard focus to `target` (first that exists). A heading or other non-interactive
 * element is made programmatically focusable (tabindex="-1", not in the tab order). Used only
 * to recover focus from a control that was removed while it had focus.
 */
export function moveFocusTo(...targets: Array<HTMLElement | null | undefined>): void {
  const target = targets.find((t): t is HTMLElement => Boolean(t && t.isConnected));
  if (!target) return;
  if (!target.matches('a[href], button, input, select, textarea, [tabindex]')) target.setAttribute('tabindex', '-1');
  target.focus();
}
