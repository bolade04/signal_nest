import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import { markAsserted, runtimeAssertedSites, type ObservedForm } from '@/test/form-defaults';
import { discoverFormSites, discoverInSource, type ProblemKind } from '@/test/form-discovery';

/**
 * FU-261-1: unit tests of the AST form discoverer on in-memory fixture sources. The
 * cross-check against the real application runs in `pages/__tests__/zod-form-defaults.test.tsx`.
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
});
