import { z } from 'zod';

/**
 * Test-only instrumentation for the useForm + zodResolver defaults contract (FU-258-2).
 *
 * A test file installs `trackZodResolver` over `@hookform/resolvers/zod` and `trackUseForm`
 * over `react-hook-form` (both wrap the REAL implementations; nothing is stubbed), then
 * renders the real application forms. Every `useForm` call whose resolver came from
 * `zodResolver(schema)` is recorded with the schema object the application built and the
 * `defaultValues` it passed — no schema or default is copied into the test.
 */

type AnyFn = (...args: never[]) => unknown;

export interface ObservedForm {
  /** `path/File.tsx:line` of the useForm call, read from the call stack (diagnostics only). */
  site: string;
  schema: z.ZodType;
  defaultValues: unknown;
  /** react-hook-form's `values` prop: externally driven (deferred) initialisation. */
  values: unknown;
}

export const observedForms: ObservedForm[] = [];

/**
 * FU-261-1: what this test file observed at RUNTIME, kept across scenarios (`observedForms`
 * is cleared per scenario). Both are private and only grow from the real `useForm` wrapper:
 * a site string cannot be added by hand, only a form object this module itself recorded.
 */
const recordedForms = new WeakSet<ObservedForm>();
const recordedSites = new Set<string>();
const assertedSites = new Set<string>();

/** Mark a recorded form as having passed the defaults assertions. Refuses any other object. */
export function markAsserted(form: ObservedForm): void {
  if (!recordedForms.has(form)) {
    throw new Error(`markAsserted: ${String(form?.site)} was not recorded by the useForm wrapper`);
  }
  assertedSites.add(form.site);
}

/** Every site the useForm wrapper recorded in this file so far (a copy). */
export function runtimeRecordedSites(): Set<string> {
  return new Set(recordedSites);
}

/** Every recorded site whose form passed the defaults assertions in this file so far (a copy). */
export function runtimeAssertedSites(): Set<string> {
  return new Set(assertedSites);
}

const schemaByResolver = new WeakMap<object, z.ZodType>();

export function trackZodResolver<F extends AnyFn>(real: F): F {
  return ((...args: Parameters<F>) => {
    const resolver = real(...args);
    if (typeof resolver === 'function') schemaByResolver.set(resolver, args[0] as unknown as z.ZodType);
    return resolver;
  }) as F;
}

function callerSite(): string {
  const frames = (new Error().stack ?? '').split('\n').slice(1);
  for (const frame of frames) {
    const match = /\/src\/((?!test\/)[^\s?:)]+\.tsx?)(?:\?[^:]*)?:(\d+)/.exec(frame);
    if (match && !frame.includes('node_modules')) return `${match[1]}:${match[2]}`;
  }
  return 'unknown site';
}

export function trackUseForm<F extends AnyFn>(real: F): F {
  return ((...args: Parameters<F>) => {
    const props = args[0] as { resolver?: unknown; defaultValues?: unknown; values?: unknown } | undefined;
    const schema = props?.resolver ? schemaByResolver.get(props.resolver as object) : undefined;
    if (schema) {
      const form: ObservedForm = { site: callerSite(), schema, defaultValues: props?.defaultValues, values: props?.values };
      recordedForms.add(form);
      recordedSites.add(form.site);
      observedForms.push(form);
    }
    return real(...args);
  }) as F;
}

/** One record per distinct (site, schema) pair; React re-renders call useForm repeatedly. */
export function distinctForms(): ObservedForm[] {
  const seen = new Map<string, ObservedForm[]>();
  for (const form of observedForms) {
    const forSite = seen.get(form.site) ?? [];
    if (!forSite.some((known) => known.schema === form.schema)) forSite.push(form);
    seen.set(form.site, forSite);
  }
  return [...seen.values()].flat();
}

/**
 * The keys a value must carry for the schema to accept it, read from the schema at runtime
 * (Zod 4): an object field is a required input when it does not accept `undefined`
 * (`isOptional()` is false), so `.optional()`, `.default()` and `.catch()` fields are exempt.
 */
export function requiredInputKeys(schema: z.ZodType): string[] {
  if (!(schema instanceof z.ZodObject)) {
    throw new Error(
      `resolver schema is a Zod "${schema.def.type}", not an object: decide deliberately how its required inputs are read`,
    );
  }
  return Object.entries(schema.shape as Record<string, z.ZodType>)
    // isOptional() parses synchronously, so a field with an async refinement throws here
    // (loudly, never a silent pass); classify such a field deliberately if one appears.
    .filter(([, field]) => !field.isOptional())
    .map(([key]) => key);
}

export type Initialisation =
  | { kind: 'static'; defaults: Record<string, unknown> }
  | { kind: 'deferred'; reason: string };

/**
 * How the form receives its first values. Only static, synchronous `defaultValues` are held
 * to the required-key rule; `values:` and async `defaultValues` are deferred initialisation
 * and are reported as such so a test must exempt them explicitly, never silently.
 */
export function initialisation(form: ObservedForm): Initialisation {
  if (form.values !== undefined) return { kind: 'deferred', reason: 'values: prop drives the form' };
  if (typeof form.defaultValues === 'function') return { kind: 'deferred', reason: 'async defaultValues' };
  if (form.defaultValues === undefined || form.defaultValues === null || typeof form.defaultValues !== 'object') {
    return { kind: 'static', defaults: {} };
  }
  return { kind: 'static', defaults: form.defaultValues as Record<string, unknown> };
}

/** Required schema input keys that the supplied defaults leave undefined. */
export function missingRequiredDefaults(form: ObservedForm, defaults: Record<string, unknown>): string[] {
  return requiredInputKeys(form.schema).filter((key) => defaults[key] === undefined);
}

/** Zod 4's wording for a missing value, e.g. "Invalid input: expected string, received undefined". */
export const RAW_UNDEFINED_ISSUE = /received undefined|Invalid input/i;
