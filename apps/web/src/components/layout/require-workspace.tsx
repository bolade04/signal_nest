import { EmptyState } from '@/components/common/states';
import { Spinner } from '@/components/ui/spinner';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// Renders children only once a workspace is resolved, passing the id down so
// feature pages never have to null-check the active workspace.
export function RequireWorkspace({
  children,
}: {
  children: (ctx: { workspaceId: string }) => React.ReactNode;
}) {
  const { workspaceId, isLoading, workspaces } = useWorkspace();

  if (workspaceId) {
    return <>{children({ workspaceId })}</>;
  }

  if (isLoading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }

  return (
    <EmptyState
      title="No workspace available"
      description={
        workspaces.length
          ? 'Select a workspace from the switcher to continue.'
          : 'This organization has no workspaces yet. Create one in Settings to begin scouting.'
      }
    />
  );
}
