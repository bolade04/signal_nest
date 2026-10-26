import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import {
  markAsserted,
  observedForms,
  runtimeAssertedSites,
  runtimeRecordedSites,
  siteFromStack,
  trackUseForm,
  trackZodResolver,
  type ObservedForm,
} from '@/test/form-defaults';
import { discoverFormSites, discoverInSource, type ProblemKind } from '@/test/form-discovery';

/**
 * FU-261-1: unit tests of the AST form discoverer on in-memory fixture sources. The
 * cross-check against the real application runs in `pages/__tests__/zod-form-defaults.test.tsx`.
 * Review r1 fixes: shared wrappers (`parameter-sourced-form-input`, F1), imports from other
 * modules (`foreign-form-import`, F2), frozen records keyed by object (F5), and the recorder's
 * stack-frame extensions (F4).
 */

const RHF = `import { useForm } from 'react-hook-form';`;
const ZR = `import { zodResolver } from '@hookform/resolvers/zod';`;

/** Join lines so a fixture's line numbers are explicit (line 1 is the first entry). */
const src = (...lines: string[]) => lines.join('\n');

function only(text: string, path = 'pages/Fixture.tsx') {
  return discoverInSource(path, text);
}

function kinds(text: string): Array<[string, ProblemKind]> {
  return only(text).problems.map((p) => [p.site, p.kind]);
}

describe('discovers supported import shapes', () => {
  it('named imports: the site is file:line of the useForm name token', () => {
    const result = only(
      src(
        RHF,
        ZR,
        `const schema = {} as never;`,
        `export function Page() {`,
        `  const form = useForm<{ a: string }>({`,
        `    resolver: zodResolver(schema),`,
        `    defaultValues: { a: '' },`,
        `  });`,
        `  return form;`,
        `}`,
      ),
    );
    expect(result.problems).toEqual([]);
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:5' }]);
  });

  it('import aliases of both packages', () => {
    const result = only(
      src(
        `import { useForm as useF } from 'react-hook-form';`,
        `import { zodResolver as zr } from '@hookform/resolvers/zod';`,
        `export function Page() {`,
        `  return useF({ resolver: zr({} as never) });`,
        `}`,
      ),
    );
    expect(result.problems).toEqual([]);
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:4' }]);
  });

  it('namespace imports of both packages, members used as values, JSX tags and types', () => {
    const result = only(
      src(
        `import * as Form from 'react-hook-form';`,
        `import * as Zr from '@hookform/resolvers/zod';`,
        `type R = Form.UseFormReturn<{ a: string }>;`,
        `export function Page() {`,
        `  const form: R = Form`,
        `    .useForm({ resolver: Zr.zodResolver({} as never), defaultValues: { a: '' } });`,
        `  return <Form.FormProvider {...form}><Form.Controller name="a" render={() => <i />} /></Form.FormProvider>;`,
        `}`,
      ),
    );
    expect(result.problems).toEqual([]);
    // The label follows the `useForm` name token, which is where V8 places the call frame.
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:6' }]);
  });

  it('a namespace for one package and a named import for the other', () => {
    const result = only(
      src(`import * as RHF from 'react-hook-form';`, ZR, `export const P = () => RHF.useForm({ resolver: zodResolver({} as never) });`),
    );
    expect(result.problems).toEqual([]);
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:3' }]);
  });

  it('two forms in one file are two sites', () => {
    const result = only(
      src(
        RHF,
        ZR,
        `export function A() { return useForm({ resolver: zodResolver({} as never) }); }`,
        `export function B() { return useForm({ resolver: zodResolver({} as never) }); }`,
      ),
    );
    expect(result.sites.map((s) => s.site)).toEqual(['pages/Fixture.tsx:3', 'pages/Fixture.tsx:4']);
  });

  it('discoverFormSites concatenates files in path order and counts them', () => {
    const form = `${RHF}\n${ZR}\nexport const P = () => useForm({ resolver: zodResolver({} as never) });`;
    const result = discoverFormSites({ 'b.tsx': form, 'a.tsx': form, 'c.ts': 'export const x = 1;' });
    expect(result.filesScanned).toBe(3);
    expect(result.sites.map((s) => s.site)).toEqual(['a.tsx:3', 'b.tsx:3']);
    expect(result.problems).toEqual([]);
  });
});

describe('a shared wrapper whose schema or defaults come from its caller is a problem (review r1 F1)', () => {
  // Fixture lines 1-3 are the two imports and a module-level schema; each body starts on line 4.
  const cases: Array<[string, string[], number]> = [
    [
      'plain function not named use… (review mutant w1)',
      [
        `export function buildZodForm(s: never, d: never) {`,
        `  return useForm({ resolver: zodResolver(s), defaultValues: d });`,
        `}`,
      ],
      5,
    ],
    [
      'generic component with destructured props',
      [
        `export function ZodForm<T>({ schema, defaults }: { schema: T; defaults: T }) {`,
        `  const f = useForm({ resolver: zodResolver(schema as never), defaultValues: defaults as never });`,
        `  return f && null;`,
        `}`,
      ],
      5,
    ],
    [
      'anonymous default export',
      [`export default function (s: never) {`, `  return useForm({ resolver: zodResolver(s) });`, `}`],
      5,
    ],
    [
      'class method',
      [`export class F {`, `  make(s: never) {`, `    return useForm({ resolver: zodResolver(s) });`, `  }`, `}`],
      6,
    ],
    [
      'capitalised UseX',
      [`export function UseLogin(s: never) {`, `  return useForm({ resolver: zodResolver(s) });`, `}`],
      5,
    ],
    [
      'memo(() => …) component reading props',
      [
        `export const ZodForm = memo((props: { s: never }) => {`,
        `  useForm({ resolver: zodResolver(props.s) });`,
        `  return null;`,
        `});`,
      ],
      5,
    ],
    [
      'defaults only, from a destructured prop',
      [
        `export function P({ d }: { d: never }) {`,
        `  return useForm({ resolver: zodResolver(moduleSchema), defaultValues: d });`,
        `}`,
      ],
      5,
    ],
    [
      'defaults shorthand that is the parameter',
      [
        `export function P(defaultValues: never) {`,
        `  return useForm({ resolver: zodResolver(moduleSchema), defaultValues });`,
        `}`,
      ],
      5,
    ],
    [
      'one default VALUE from a prop',
      [
        `export function P(props: { email: string }) {`,
        `  return useForm({ resolver: zodResolver(moduleSchema), defaultValues: { email: props.email } });`,
        `}`,
      ],
      5,
    ],
    [
      'defaults spread from a prop',
      [
        `export function P(props: { d: object }) {`,
        `  return useForm({ resolver: zodResolver(moduleSchema), defaultValues: { ...props.d } });`,
        `}`,
      ],
      5,
    ],
    [
      'through destructuring and a variable alias',
      [
        `export function P(props: { schema: never }) {`,
        `  const { schema } = props;`,
        `  const s2 = schema;`,
        `  return useForm({ resolver: zodResolver(s2) });`,
        `}`,
      ],
      7,
    ],
    [
      'through a method call on the prop (its receiver)',
      [
        `export function P(props: { schema: { strict(): never } }) {`,
        `  return useForm({ resolver: zodResolver(props.schema.strict()) });`,
        `}`,
      ],
      5,
    ],
    [
      'through ?: and ?? branches',
      [
        `export function P(props: { s?: never; d?: never }, x: boolean) {`,
        `  return useForm({ resolver: zodResolver(x ? moduleSchema : props.s!), defaultValues: props.d ?? {} });`,
        `}`,
      ],
      5,
    ],
    [
      '`this` in a class',
      [
        `export class F {`,
        `  schema = {} as never;`,
        `  make() {`,
        `    return useForm({ resolver: zodResolver(this.schema) });`,
        `  }`,
        `}`,
      ],
      7,
    ],
    [
      '`arguments`',
      [`export function P() {`, `  return useForm({ resolver: zodResolver(arguments[0] as never) });`, `}`],
      5,
    ],
    [
      'a parameter of an OUTER function, read in a nested one',
      [
        `export function Outer(p: { s: never }) {`,
        `  const Inner = () => useForm({ resolver: zodResolver(p.s) });`,
        `  return Inner;`,
        `}`,
      ],
      5,
    ],
  ];

  it.each(cases)('%s', (_name, body, line) => {
    const text = src(RHF, ZR, `const moduleSchema = {} as never;`, ...body);
    const result = only(text);
    expect(result.sites).toEqual([]);
    expect(kinds(text)).toEqual([[`pages/Fixture.tsx:${line}`, 'parameter-sourced-form-input']]);
    expect(result.problems[0]!.detail).toMatch(/comes from .* shared wrapper/);
  });

  it('the detail names the source and the owner', () => {
    const text = src(
      RHF,
      ZR,
      `export function ZodForm({ schema }: { schema: never }) {`,
      `  return useForm({ resolver: zodResolver(schema) });`,
      `}`,
    );
    expect(only(text).problems.map((p) => p.detail)).toEqual([
      'zodResolver schema comes from parameter "schema" of "ZodForm": a shared wrapper, so one site label would cover every caller',
    ]);
  });

  it('a use… name wrapped in memo(...) is a custom hook wrapper', () => {
    const text = src(
      RHF,
      ZR,
      `const s = {} as never;`,
      `export const useThing = memo(() => useForm({ resolver: zodResolver(s) }));`,
    );
    expect(kinds(text)).toEqual([['pages/Fixture.tsx:4', 'custom-hook-wrapper']]);
  });

  it('props that do not feed the schema or defaults, module schemas, and memoised local schemas stay sites', () => {
    const text = src(
      RHF,
      ZR,
      `import { useMemo } from 'react';`,
      `const moduleSchema = {} as never;`,
      // RegisterForm / SignInForm shape: props exist; the schema is module-level, the defaults literal.
      `export function RegisterForm({ error, onSubmit }: { error: string; onSubmit: () => void }) {`,
      `  const alias = moduleSchema;`,
      `  const form = useForm({ resolver: zodResolver(alias), defaultValues: { full_name: '', password: '' } });`,
      `  return [form, error, onSubmit];`,
      `}`,
      // InviteMemberDialog shape: a memoised schema, and a default read from a memoised local,
      // both computed from a prop through useMemo (a call): not followed, by design.
      `export function InviteMemberDialog({ actorRole }: { actorRole: string }) {`,
      `  const roles = useMemo(() => [actorRole], [actorRole]);`,
      `  const schema = useMemo(() => ({ roles }) as never, [roles]);`,
      `  return useForm({ resolver: zodResolver(schema), defaultValues: { email: '', role: roles[roles.length - 1] } });`,
      `}`,
    );
    const result = only(text);
    expect(result.problems).toEqual([]);
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:7' }, { site: 'pages/Fixture.tsx:13' }]);
  });
});

describe('useForm / zodResolver imported from any other module is a problem (review r1 F2)', () => {
  const cases: Array<[string, string, Array<[string, ProblemKind]>]> = [
    [
      'named imports from a re-exporting module outside the glob (review mutant w2)',
      src(
        `import { useForm, zodResolver } from '@/test/forms-reexport';`,
        `export const P = () => useForm({ resolver: zodResolver({} as never) });`,
      ),
      [
        ['pages/Fixture.tsx:1', 'foreign-form-import'],
        ['pages/Fixture.tsx:1', 'foreign-form-import'],
      ],
    ],
    [
      'an aliased import from a module outside src/',
      src(`import { useForm as useF } from '../../shared/forms';`, `export const P = () => useF({});`),
      [['pages/Fixture.tsx:1', 'foreign-form-import']],
    ],
    [
      'a name imported from the other supported package',
      src(`import { zodResolver } from 'react-hook-form';`, `import { useForm } from '@hookform/resolvers/zod';`),
      [
        ['pages/Fixture.tsx:1', 'foreign-form-import'],
        ['pages/Fixture.tsx:2', 'foreign-form-import'],
      ],
    ],
    [
      'namespace and default import members, and destructuring',
      src(
        `import * as F from '@/test/forms-reexport';`,
        `import forms from 'shared-forms';`,
        `export const P = () => F.useForm({ resolver: forms.zodResolver({} as never) });`,
        `export const Q = F['zodResolver'];`,
        `const { useForm: u } = F;`,
        `export { u };`,
      ),
      [
        ['pages/Fixture.tsx:3', 'foreign-form-import'],
        ['pages/Fixture.tsx:3', 'foreign-form-import'],
        ['pages/Fixture.tsx:4', 'foreign-form-import'],
        ['pages/Fixture.tsx:5', 'foreign-form-import'],
      ],
    ],
  ];

  it.each(cases)('%s', (_name, text, expected) => {
    const result = only(text);
    expect(result.sites).toEqual([]);
    expect(kinds(text)).toEqual(expected);
    for (const p of result.problems) expect(p.detail).toMatch(/not from its package/);
  });

  it('other names, type-only imports and other members of other modules are not problems', () => {
    const text = src(
      `import { useFormContext, Controller } from '@/components/forms';`,
      `import type { useForm } from '@/types/forms';`,
      `import { type zodResolver as Z } from '@/types/forms';`,
      `import * as api from '@/api/endpoints';`,
      `export const P = () => [useFormContext, Controller, api.createInvitation];`,
      `export type T = [typeof useForm, Z];`,
    );
    expect(only(text)).toEqual({ sites: [], problems: [] });
  });
});

describe('comments, strings and non-forms do not count', () => {
  it('useForm( / zodResolver( in comments, strings, templates, regexes and JSX text are not sites or problems', () => {
    const result = only(
      src(
        RHF,
        ZR,
        `// const a = useForm({ resolver: zodResolver(schema) });`,
        `/* const b = useForm({ resolver: zodResolver(schema) }); */`,
        `/** @example useForm({ resolver: zodResolver(schema) }) */`,
        `const s = 'useForm({ resolver: zodResolver(schema) })';`,
        `const d = "zodResolver(x)";`,
        'const t = `useForm({ resolver: zodResolver(${s}) })`;',
        `const r = /useForm\\(\\{ resolver: zodResolver\\(/;`,
        `export const P = () => <p title="useForm(">useForm({'{'} resolver: zodResolver(x) {'}'})</p>;`,
        `export const all = [s, d, t, r];`,
      ),
    );
    expect(result).toEqual({ sites: [], problems: [] });
  });

  it('the same comment and string text beside one real form yields exactly that one site', () => {
    const result = only(
      src(
        RHF,
        ZR,
        `// useForm({ resolver: zodResolver(a) })`,
        `const s = 'useForm({ resolver: zodResolver(b) })';`,
        `export const P = () => useForm({ resolver: zodResolver({} as never), defaultValues: { s } });`,
      ),
    );
    expect(result.sites).toEqual([{ site: 'pages/Fixture.tsx:5' }]);
    expect(result.problems).toEqual([]);
  });

  it('a local useForm / zodResolver that is not imported from the packages is not a form', () => {
    const result = only(
      src(
        `function useForm(o: unknown) { return o; }`,
        `function zodResolver(s: unknown) { return s; }`,
        `export const P = () => useForm({ resolver: zodResolver({}) });`,
      ),
    );
    expect(result).toEqual({ sites: [], problems: [] });
  });

  it('type-only imports, typeof queries, and useForm without a resolver are neither sites nor problems', () => {
    const result = only(
      src(
        `import type { useForm as T } from 'react-hook-form';`,
        `import { type zodResolver as Z } from '@hookform/resolvers/zod';`,
        RHF,
        `type F = ReturnType<typeof useForm>;`,
        `export const P = (): F => useForm({ defaultValues: { a: '' } });`,
        `export const Q = () => useForm();`,
        `export type Both = [T, Z];`,
      ),
    );
    expect(result).toEqual({ sites: [], problems: [] });
  });
});

describe('unsupported shapes are reported by name and site, never skipped', () => {
  const cases: Array<[string, string, Array<[string, ProblemKind]>]> = [
    [
      'default import',
      src(`import RHF from 'react-hook-form';`, `export const x = RHF;`),
      [['pages/Fixture.tsx:1', 'default-import']],
    ],
    [
      '{ default as X } import',
      src(`import { default as zr } from '@hookform/resolvers/zod';`, `export const x = zr;`),
      [['pages/Fixture.tsx:1', 'default-import']],
    ],
    [
      'deep or sibling specifier',
      src(`import { useForm } from 'react-hook-form/dist/index.esm.mjs';`, `import { yupResolver } from '@hookform/resolvers/yup';`),
      [
        ['pages/Fixture.tsx:1', 'unsupported-specifier'],
        ['pages/Fixture.tsx:2', 'unsupported-specifier'],
      ],
    ],
    [
      'dynamic import of a package, and with a non-literal specifier',
      src(`const name = 'x';`, `export const a = import('react-hook-form');`, `export const b = import(name);`, `export const c = import('./Other');`),
      [
        ['pages/Fixture.tsx:2', 'dynamic-import'],
        ['pages/Fixture.tsx:3', 'dynamic-import'],
      ],
    ],
    [
      'require and import-equals',
      src(`const a = require('@hookform/resolvers/zod');`, `import b = require('react-hook-form');`, `export { a, b };`),
      [
        ['pages/Fixture.tsx:1', 'require'],
        ['pages/Fixture.tsx:2', 'require'],
      ],
    ],
    [
      're-export from a package',
      src(`export { useForm } from 'react-hook-form';`, `export * from '@hookform/resolvers/zod';`, `export * as R from 'react-hook-form';`),
      [
        ['pages/Fixture.tsx:1', 're-export'],
        ['pages/Fixture.tsx:2', 're-export'],
        ['pages/Fixture.tsx:3', 're-export'],
      ],
    ],
    [
      're-export of an imported binding or namespace',
      src(RHF, `import * as Z from '@hookform/resolvers/zod';`, `export { useForm as useAppForm, Z };`, `export default useForm;`),
      [
        ['pages/Fixture.tsx:3', 're-export'],
        ['pages/Fixture.tsx:3', 're-export'],
        ['pages/Fixture.tsx:4', 're-export'],
      ],
    ],
    [
      'aliasing via variable assignment',
      src(RHF, ZR, `const f = useForm;`, `const z2 = zodResolver;`, `export const P = () => f({ resolver: z2({} as never) });`),
      [
        ['pages/Fixture.tsx:3', 'value-alias'],
        ['pages/Fixture.tsx:4', 'value-alias'],
      ],
    ],
    [
      'namespace member aliased, and passed as an argument or into an object',
      src(
        `import * as RHF from 'react-hook-form';`,
        ZR,
        `const f = RHF.useForm;`,
        `export const g = [zodResolver].map((x) => x);`,
        `export const h = { zodResolver };`,
        `export const P = () => f({});`,
      ),
      [
        ['pages/Fixture.tsx:3', 'value-alias'],
        ['pages/Fixture.tsx:4', 'value-alias'],
        ['pages/Fixture.tsx:5', 'value-alias'],
      ],
    ],
    [
      'namespace escape: destructured, element access, passed',
      src(
        `import * as RHF from 'react-hook-form';`,
        `const { useForm: uf } = RHF;`,
        `const e = RHF['useForm'];`,
        `export const k = Object.keys(RHF);`,
        `export { uf, e };`,
      ),
      [
        ['pages/Fixture.tsx:2', 'namespace-escape'],
        ['pages/Fixture.tsx:3', 'namespace-escape'],
        ['pages/Fixture.tsx:4', 'namespace-escape'],
      ],
    ],
    [
      'shadowed binding',
      src(RHF, `export function P(useForm: () => void) { return useForm(); }`),
      [['pages/Fixture.tsx:2', 'shadowed-binding']],
    ],
    [
      'non-literal options object',
      src(RHF, ZR, `const opts = { resolver: zodResolver({} as never) };`, `export const P = () => useForm(opts);`),
      [
        ['pages/Fixture.tsx:4', 'options-not-literal'],
        ['pages/Fixture.tsx:3', 'zod-resolver-outside-useform'],
      ],
    ],
    [
      'spread and computed key in the options literal',
      src(
        RHF,
        ZR,
        `const base = {};`,
        `export const P = () => useForm({ ...base, resolver: zodResolver({} as never) });`,
        `export const Q = () => useForm({ ['resolver']: zodResolver({} as never) });`,
      ),
      [
        ['pages/Fixture.tsx:4', 'options-spread'],
        ['pages/Fixture.tsx:5', 'options-computed-key'],
        ['pages/Fixture.tsx:5', 'zod-resolver-outside-useform'],
      ],
    ],
    [
      'resolver held in a variable, wrapped in a lambda, chosen by a ternary, or shorthand',
      src(
        RHF,
        ZR,
        `const r = zodResolver({} as never);`,
        `export const A = () => useForm({ resolver: r });`,
        `export const B = () => useForm({ resolver: (v, c, o) => zodResolver({} as never)(v, c, o) });`,
        `export const C = (x: boolean) => useForm({ resolver: x ? zodResolver({} as never) : undefined });`,
        `export const D = (resolver: never) => useForm({ resolver });`,
      ),
      [
        ['pages/Fixture.tsx:4', 'resolver-not-direct-zod-call'],
        ['pages/Fixture.tsx:5', 'resolver-not-direct-zod-call'],
        ['pages/Fixture.tsx:6', 'resolver-not-direct-zod-call'],
        ['pages/Fixture.tsx:7', 'resolver-not-direct-zod-call'],
        ['pages/Fixture.tsx:3', 'zod-resolver-outside-useform'],
        ['pages/Fixture.tsx:5', 'zod-resolver-outside-useform'],
        ['pages/Fixture.tsx:6', 'zod-resolver-outside-useform'],
      ],
    ],
    [
      'zodResolver whose enclosing call is not a recognised useForm',
      src(ZR, `const useMyForm = (o: unknown) => o;`, `export const P = () => useMyForm({ resolver: zodResolver({} as never) });`),
      [['pages/Fixture.tsx:3', 'zod-resolver-outside-useform']],
    ],
    [
      'custom wrapper hook (function declaration and arrow)',
      src(
        RHF,
        ZR,
        `export function useAppForm(s: never) { return useForm({ resolver: zodResolver(s) }); }`,
        `export const useOtherForm = (s: never) => useForm({ resolver: zodResolver(s) });`,
      ),
      [
        ['pages/Fixture.tsx:3', 'custom-hook-wrapper'],
        ['pages/Fixture.tsx:4', 'custom-hook-wrapper'],
      ],
    ],
  ];

  it.each(cases)('%s', (_name, text, expected) => {
    const result = only(text);
    expect(result.sites).toEqual([]);
    expect(kinds(text)).toEqual(expected);
    for (const p of result.problems) expect(p.detail.length).toBeGreaterThan(0);
  });
});

describe('coverage cannot be granted by naming a site (FU-261-1)', () => {
  it('markAsserted refuses a form object the useForm wrapper did not record', () => {
    const forged: ObservedForm = { site: 'pages/NotFound.tsx:5', schema: z.object({}), defaultValues: {}, values: undefined };
    expect(() => markAsserted(forged)).toThrow(/was not recorded by the useForm wrapper/);
    expect(runtimeAssertedSites().has('pages/NotFound.tsx:5')).toBe(false);
  });

  it('the asserted-site set handed out is a copy', () => {
    runtimeAssertedSites().add('pages/NotFound.tsx:5');
    expect(runtimeAssertedSites().has('pages/NotFound.tsx:5')).toBe(false);
  });

  it('a recorded form is frozen, and coverage follows the recorded site, not form.site (review r1 F5)', () => {
    const zodResolver = trackZodResolver((schema: unknown) => () => schema);
    const useForm = trackUseForm((options: unknown) => options);
    useForm({ resolver: zodResolver(z.object({ a: z.string() })), defaultValues: { a: '' } });
    const form = observedForms[observedForms.length - 1]!;
    // Called from src/test/, which the recorder skips: no application frame is on the stack.
    expect(form.site).toBe('unknown site');
    expect(Object.isFrozen(form)).toBe(true);
    expect(() => Object.assign(form, { site: 'pages/FeedbackPage.tsx:8' })).toThrow(TypeError);
    expect(() => Object.defineProperty(form, 'site', { value: 'pages/FeedbackPage.tsx:8' })).toThrow(TypeError);
    markAsserted(form);
    expect(runtimeAssertedSites()).toEqual(new Set(['unknown site']));
    for (const site of runtimeAssertedSites()) expect(runtimeRecordedSites().has(site)).toBe(true);
  });
});

describe('the recorder reads the first application frame of the stack (review r1 F4)', () => {
  const stack = (...frames: string[]) => ['Error', ...frames.map((frame) => `    at ${frame}`)].join('\n');

  it.each([
    ['tsx with a vite query string', 'Page (/w/apps/web/src/pages/A.tsx?t=123:12:5)', 'pages/A.tsx:12'],
    ['ts', 'hook (/w/apps/web/src/lib/a.ts:3:1)', 'lib/a.ts:3'],
    ['jsx', 'Page (/w/apps/web/src/pages/B.jsx:8:16)', 'pages/B.jsx:8'],
    ['js, anonymous frame', '/w/apps/web/src/pages/C.js:4:2', 'pages/C.js:4'],
    ['mjs', 'x (/w/apps/web/src/a/D.mjs:1:1)', 'a/D.mjs:1'],
    ['cts', 'x (/w/apps/web/src/a/E.cts:2:1)', 'a/E.cts:2'],
  ])('%s', (_name, frame, site) => {
    expect(siteFromStack(stack(frame))).toBe(site);
  });

  it('skips src/test/ and node_modules frames, and reports an unknown site when nothing else is left', () => {
    const skipped = [
      'callerSite (/w/apps/web/src/test/form-defaults.ts:70:3)',
      'useForm (/w/apps/web/node_modules/react-hook-form/dist/src/index.esm.mjs:9:1)',
    ];
    expect(siteFromStack(stack(...skipped, 'Page (/w/apps/web/src/pages/F.jsx:6:9)'))).toBe('pages/F.jsx:6');
    expect(siteFromStack(stack(...skipped))).toBe('unknown site');
    expect(siteFromStack(stack('Page (/w/apps/web/src/pages/G.css:1:1)'))).toBe('unknown site');
  });
});
