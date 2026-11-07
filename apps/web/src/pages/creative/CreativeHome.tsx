import { PenLine } from 'lucide-react';
import { Link } from 'react-router-dom';
import { EmptyState } from '@/components/common/states';
import { Button } from '@/components/ui/button';

/**
 * The `/creative` landing page (I-5C), the target of the sidebar entry.
 *
 * Drafts belong to a recommendation brief, and the API lists them per brief only
 * (`GET /workspaces/{ws}/briefs/{brief_id}/drafts`); there is no workspace-wide draft list. So
 * this page says where drafts live and links there. A brief's drafts are at
 * `/creative/:briefId`, reached from the brief panel's "Open drafts" link.
 */
export function CreativeHomePage() {
  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <PenLine className="size-5 text-primary" /> Creative drafts
        </h1>
        <p className="text-sm text-muted-foreground">
          Marketing text generated from an approved recommendation brief, checked against your
          claims library, and approved by a reviewer.
        </p>
      </header>
      <EmptyState
        icon={PenLine}
        title="Drafts live with their brief"
        description="Open an opportunity, then choose Open drafts on its recommendation brief. Editors generate drafts from an approved brief; reviewers approve them."
        action={
          <Button asChild>
            <Link to="/opportunities">Go to opportunities</Link>
          </Button>
        }
      />
    </div>
  );
}
