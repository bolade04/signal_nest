import { Button } from '@/components/ui/button';

/**
 * The seeded demo account shortcut — development builds only.
 *
 * P6-UI-008: these literals used to sit at module scope in `SignIn.tsx`, so they
 * were compiled into the production bundle and printed in the page body on every
 * visit. A published credential for a tenant-owner account has no business in a
 * browser artifact, whether or not that account exists in any deployment.
 *
 * Everything demo-related lives here so the whole module drops out of a production
 * build: its only reference is inside an `import.meta.env.DEV` branch, which Vite
 * folds to `false` at transform time, leaving nothing to import.
 *
 * Two constraints keep that true, and both are load-bearing:
 *
 *  - Import this module STATICALLY. A dynamic `import()` is code-split before the
 *    branch is folded, so the credentials would ship in a chunk of their own.
 *  - Keep the module side-effect free. A top-level side effect makes the bundler
 *    retain the module as a shell even once the component is unreferenced.
 *
 * The guard is the mechanism; the real control is the post-build scan in
 * `scripts/assert-no-demo-credentials.mjs`, which fails the build if either
 * literal reaches `dist/`.
 */

const DEMO_EMAIL = 'demo@signalnest.dev';
const DEMO_PASSWORD = 'demo1234';

export function DemoSignInShortcut({
  onUse,
  disabled,
}: {
  /** Fills the sign-in form with the seeded credentials and submits it. */
  onUse: (email: string, password: string) => void;
  disabled: boolean;
}) {
  return (
    <>
      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={() => onUse(DEMO_EMAIL, DEMO_PASSWORD)}
        disabled={disabled}
      >
        Use demo account
      </Button>
      <p className="rounded-md bg-muted/60 px-3 py-2 text-center text-xs text-muted-foreground">
        Demo login: {DEMO_EMAIL} · {DEMO_PASSWORD}
      </p>
    </>
  );
}
