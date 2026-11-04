import { LoadingRows } from '@/components/common/states';
import { NotFoundPage } from '@/pages/NotFound';
import { useBriefCapability } from '@/pages/opportunities/useBriefs';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { ApprovalQueuePage } from './ApprovalQueue';

/**
 * The `/approvals` route element (mounted at the I-5B integration cycle, Phase 5 plan §6.3).
 *
 * Approvals share the briefs' one dark switch (`recommendation_briefs_enabled`, decided per
 * workspace by the resolver). While it does not resolve enabled for the active workspace the
 * route renders the not-found page in place — the same answer as any unknown path, as
 * `RequireOperator` does for operator routes — so a dark workspace shows no trace of the
 * feature and sends no approval request. The decision comes from the workspace-effective
 * reflection (`GET /workspaces/{workspace_id}/brief-capability`), which every member may
 * read; an unresolved or failed lookup never renders the queue.
 *
 * Once enabled, `ApprovalQueuePage` applies the role rule itself: a reviewer gets the queue;
 * any other member gets the explanation and no queue request. UX only — every approval
 * endpoint re-decides role and capability on the server.
 */
export function ApprovalsRoute() {
  const { workspaceId } = useWorkspace();
  const capability = useBriefCapability(workspaceId ?? '');
  if (!workspaceId || capability.status === 'loading') return <LoadingRows rows={2} />;
  if (!capability.isEnabled) return <NotFoundPage />;
  return <ApprovalQueuePage />;
}
