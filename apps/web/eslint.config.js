// Flat ESLint configuration for @signalnest/web (ESLint 10).
// Replaces the legacy .eslintrc.cjs. Behavior is a faithful translation of the
// previous config: ESLint recommended + typescript-eslint recommended
// (non-type-aware) + React Hooks recommended + React Refresh, with the same
// project-specific rule customizations, globals and ignore patterns.
import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';

export default tseslint.config(
  // Ignore patterns (previously .eslintrc `ignorePatterns`).
  {
    ignores: [
      'dist',
      'src/api/schema.d.ts',
      '**/*.config.ts',
      '**/*.config.js',
      'eslint.config.js',
    ],
  },

  // Base presets, applied to the linted TypeScript sources.
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,

  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: { 'react-refresh': reactRefresh },
    rules: {
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/consistent-type-imports': ['warn', { prefer: 'type-imports' }],
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },

  // Design-system primitives (shadcn/ui) deliberately co-locate re-exported
  // Radix primitive *components* (e.g. `export const Dialog = DialogPrimitive.Root`)
  // with their styled wrappers. react-refresh 0.5.3 can't statically prove those
  // member-expression re-exports are components, so it warns even though Fast
  // Refresh already remounts these modules correctly. The rule is a dev-only HMR
  // hint and is architecturally inappropriate for this primitives directory; it
  // stays fully enforced for all application code.
  {
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: { 'react-refresh/only-export-components': 'off' },
  },

  // Tests may use `any` freely (previously an .eslintrc `overrides` entry).
  {
    files: ['**/*.test.ts', '**/*.test.tsx', 'src/test/**'],
    rules: { '@typescript-eslint/no-explicit-any': 'off' },
  },
);
