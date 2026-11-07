import { Outlet } from 'react-router-dom';
import { LoadingRows } from '@/components/common/states';
import { NotFoundPage } from '@/pages/NotFound';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { useCreativeCapability } from './useCreative';

/**
 * The layout route for every `/creative` page (mounted at the I-5C integration cycle, Phase 5
 * plan §6.3 and the 5C sub-plan's shared-surface section).
 *
 * While `creative_generation` does not resolve enabled for the active workspace, or its
 * workspace-effective reflection (`GET /workspaces/{workspace_id}/creative-capability`) cannot
 * be read, the not-found page is rendered in place: the same answer as any unknown path, as
 * `ApprovalsRoute` and `RequireOperator` do. A dark workspace therefore shows no trace of the
 * feature and sends no draft request. Drafts are readable by every member, so no role is
 * refused here; each page applies the API's role rules to its controls. UX only: every draft
 * route re-decides capability and role on the server.
 */
export function CreativeRoute() {
  const { workspaceId } = useWorkspace();
  const capability = useCreativeCapability(workspaceId ?? '');
  if (!workspaceId || capability.status === 'loading') return <LoadingRows rows={2} />;
  if (!capability.isEnabled) return <NotFoundPage />;
  return <Outlet />;
}
