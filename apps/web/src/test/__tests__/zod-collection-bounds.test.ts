import { describe, expect, it } from 'vitest';
import { z } from 'zod';

// GHSA-mrhx-c7xc-m3w5 / CVE-2023-54404 (HIGH, CWE-770, runtime scope): zod before 4.6.6 can
// exhaust memory while validating oversized constrained collections, because it appends one
// issue per element instead of stopping at the collection's own bound. On 3.25.76 the schema
// below yields n+1 issues (101 for n=100, 5001 for n=5000); on 4.6.7 it yields exactly one.
//
// These cases pin the *bounded* property, not the crash: a deliberately small n is used and the
// assertion is that the issue list does not grow with the input. Reproducing the exhaustion
// itself is explicitly out of scope — an out-of-memory run proves nothing a bound does not, and
// would make the suite hostile to the machine it runs on.
describe('oversized constrained collections are rejected without a per-element issue list', () => {
  // Small, and far apart, so per-element growth cannot hide inside a constant.
  const SMALL = 100;
  const LARGE = 5_000;

  /** A bounded array whose element schema counts how many elements were actually parsed. */
  function counting() {
    const state = { parsed: 0 };
    const element = z.custom<string>((value) => {
      state.parsed += 1;
      return typeof value === 'string';
    }, 'not a string');
    // `.max()` must lead: the fix hoists a leading size bound ahead of element parsing, and a
    // bound placed after another check is not hoisted. See the limitation note at the foot.
    return { state, schema: z.array(element).max(10) };
  }

  const reject = (schema: z.ZodType, length: number) => {
    const result = schema.safeParse(Array.from({ length }, () => 1));
    expect(result.success).toBe(false);
    /* v8 ignore next */
    if (result.success) throw new Error('unreachable: oversized input must be rejected');
    return result.error.issues;
  };

  it('rejects an oversized collection', () => {
    const { schema } = counting();
    expect(schema.safeParse(Array.from({ length: LARGE }, () => 'ok')).success).toBe(false);
  });

  it('does not grow the issue list with the input size', () => {
    const small = reject(counting().schema, SMALL);
    const large = reject(counting().schema, LARGE);
    expect(large).toHaveLength(small.length);
  });

  it('reports exactly the collection bound it violated', () => {
    // The vulnerable parser returned n+1 issues here (101 at n=100, 5001 at n=5000). 4.6.7
    // reports the single `too_big` issue the constraint itself describes.
    const issues = reject(counting().schema, LARGE);
    expect(issues).toHaveLength(1);
    expect(issues[0]?.code).toBe('too_big');
  });

  it('never parses a single element of an oversized collection', () => {
    // The actual mechanism of the fix, and the assertion a `success === false` check misses:
    // the size bound short-circuits before any element is visited, so the work — and the issue
    // list — cannot scale with the input at all.
    const { state, schema } = counting();
    reject(schema, LARGE);
    expect(state.parsed).toBe(0);
  });

  it('still parses and reports every element when the collection is within its bound', () => {
    // Anti-evasion: a parser that simply refused all arrays, or an element schema that never
    // ran, would satisfy the cases above. Inside the bound, element parsing must still happen
    // and must still produce one issue per offending element.
    const { state, schema } = counting();
    const issues = reject(schema, 2);
    expect(state.parsed).toBe(2);
    expect(issues).toHaveLength(2);
    expect(issues.every((issue) => issue.code === 'custom')).toBe(true);
  });
});

// The migration must not change what a person sees. These pin the exact messages and their order
// for every construct the app's forms actually use; all four behave identically on 3.25.76 and
// 4.6.7, so a future change to zod or to the resolver that alters them fails here instead of in a
// form. The resolver surfaces the first issue for a path, so order is load-bearing.
describe('form validation behaviour the migration preserves', () => {
  const email = z.string().min(1, 'Email is required').email('Enter a valid email address');

  it('reports the required message first for empty input', () => {
    const result = email.safeParse('');
    expect(result.success).toBe(false);
    /* v8 ignore next */
    if (result.success) return;
    expect(result.error.issues.map((issue) => issue.message)).toEqual([
      'Email is required',
      'Enter a valid email address',
    ]);
  });

  it('reports only the format message for a malformed address', () => {
    const result = email.safeParse('nope');
    expect(result.success).toBe(false);
    /* v8 ignore next */
    if (result.success) return;
    expect(result.error.issues.map((issue) => issue.message)).toEqual([
      'Enter a valid email address',
    ]);
  });

  it('accepts a well-formed address', () => {
    expect(email.safeParse('person@example.com').success).toBe(true);
  });

  // `.trim()` before `.min(1)` is the Invitations schema's shape, and it is the only construct
  // here whose zod 4 equivalence was not already pinned by an existing exact-string assertion:
  // the trim has to apply before the length check, or whitespace would satisfy `.min(1)`.
  it('trims before measuring length, and returns the trimmed value', () => {
    const email = z.string().trim().min(1, 'Enter an email address').email('Enter a valid email address');

    const blank = email.safeParse('   ');
    expect(blank.success).toBe(false);
    /* v8 ignore next */
    if (blank.success) return;
    expect(blank.error.issues[0]?.message).toBe('Enter an email address');

    expect(email.safeParse('  person@example.com  ')).toEqual({
      success: true,
      data: 'person@example.com',
    });
  });

  it('keeps a cross-field refinement on its stated path', () => {
    const schema = z
      .object({ new_password: z.string(), confirm_password: z.string() })
      .refine((values) => values.new_password === values.confirm_password, {
        message: 'The passwords do not match',
        path: ['confirm_password'],
      });
    const result = schema.safeParse({ new_password: 'a', confirm_password: 'b' });
    expect(result.success).toBe(false);
    /* v8 ignore next */
    if (result.success) return;
    expect(result.error.issues.map((issue) => ({ path: issue.path, message: issue.message }))).toEqual(
      [{ path: ['confirm_password'], message: 'The passwords do not match' }],
    );
  });

  it('keeps z.custom predicate and message', () => {
    const role = z.custom<'OWNER' | 'MEMBER'>(
      (value) => value === 'OWNER' || value === 'MEMBER',
      'Choose a role',
    );
    expect(role.safeParse('OWNER').success).toBe(true);
    const result = role.safeParse('NOPE');
    expect(result.success).toBe(false);
    /* v8 ignore next */
    if (result.success) return;
    expect(result.error.issues.map((issue) => issue.message)).toEqual(['Choose a role']);
  });
});

// Limitation, recorded deliberately rather than implied away by the cases above.
//
// The 4.6.6 fix hoists a *leading* `.max()` / `.length()` / `.size()` ahead of element parsing.
// It therefore does not bound two shapes:
//   - a container with no size constraint at all — `z.array(x)` is parsed element by element as
//     before, and NVD's 2026-10-22 revision of CVE-2023-54404 states that no released version
//     remediates the unbounded case;
//   - a bound that does not lead, e.g. `z.array(x).refine(f).max(10)`, which is not hoisted.
//
// So this upgrade is necessary and sufficient for the advisory as scoped, and for the adopted
// gate, but it is not a complete fix for the defect class. Nothing in this application is
// exposed either way: `z.array`, `z.set`, `z.map` and `z.record` appear nowhere in apps/web/src
// (every schema is an object of strings), and the API is Python. Should a collection schema ever
// be introduced here, give it a leading size bound.
