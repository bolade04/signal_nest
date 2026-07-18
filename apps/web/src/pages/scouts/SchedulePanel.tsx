import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { CalendarClock, Pause, Play, Trash2 } from 'lucide-react';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ScheduleInterval, ScoutScheduleOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { ErrorState, LoadingRows } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { formatDateTime, formatRelative, titleCase } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { useScheduleActions } from './useScheduleActions';

// Owner/admin/marketer may mutate; view-only roles get read-only display. This
// mirrors the server-side EDITORS gate — the API is the real authority, this
// only hides controls the user could not use anyway.
const EDITOR_ROLES = new Set(['owner', 'admin', 'marketer']);

const STATE_META: Record<
  string,
  { label: string; intent: 'success' | 'warning' | 'muted'; hint?: string }
> = {
  active: { label: 'Active', intent: 'success' },
  paused: { label: 'Paused', intent: 'muted' },
  activation_required: {
    label: 'Activation required',
    intent: 'warning',
    hint: 'Recurring scouting is enabled, but this schedule has not been activated yet. Activate it to start the recurring runs.',
  },
};

export function SchedulePanel({
  workspaceId,
  requestId,
}: {
  workspaceId: string;
  requestId: string;
}) {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role;
  const canEdit = role ? EDITOR_ROLES.has(role) : false;

  const actions = useScheduleActions(workspaceId, requestId);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const query = useQuery({
    queryKey: queryKeys.scoutSchedule(workspaceId, requestId),
    queryFn: ({ signal }) => api.getScoutSchedule(workspaceId, requestId, signal),
    // A 404 is the documented "no schedule yet" signal, not a transient failure.
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });

  const noSchedule = query.error instanceof ApiError && query.error.status === 404;
  const busy =
    actions.create.isPending ||
    actions.pause.isPending ||
    actions.resume.isPending ||
    actions.remove.isPending;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <CalendarClock className="size-4 text-muted-foreground" /> Recurring schedule
        </CardTitle>
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <LoadingRows rows={1} />
        ) : query.isError && !noSchedule ? (
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        ) : noSchedule || !query.data ? (
          <NoSchedule
            canEdit={canEdit}
            busy={busy}
            onCreate={(interval) => actions.create.mutate(interval)}
          />
        ) : (
          <ScheduleView
            schedule={query.data}
            canEdit={canEdit}
            busy={busy}
            onPause={() => actions.pause.mutate()}
            onActivate={() => actions.resume.mutate()}
            onDelete={() => setConfirmDelete(true)}
          />
        )}
      </CardContent>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete schedule?"
        description="Recurring runs will stop. This does not affect runs already in progress, and you can create a new schedule later."
        confirmLabel="Delete schedule"
        destructive
        onConfirm={() => actions.remove.mutateAsync()}
      />
    </Card>
  );
}

function NoSchedule({
  canEdit,
  busy,
  onCreate,
}: {
  canEdit: boolean;
  busy: boolean;
  onCreate: (interval: ScheduleInterval) => void;
}) {
  return (
    <div className="space-y-3 text-sm">
      <p className="text-muted-foreground">
        No recurring schedule. Runs stay manual until you set one up.
      </p>
      {canEdit ? (
        <div className="flex flex-wrap gap-2">
          <Button size="sm" onClick={() => onCreate('daily')} disabled={busy}>
            Schedule daily
          </Button>
          <Button size="sm" variant="outline" onClick={() => onCreate('weekly')} disabled={busy}>
            Schedule weekly
          </Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          You do not have permission to manage this schedule.
        </p>
      )}
    </div>
  );
}

function ScheduleView({
  schedule,
  canEdit,
  busy,
  onPause,
  onActivate,
  onDelete,
}: {
  schedule: ScoutScheduleOut;
  canEdit: boolean;
  busy: boolean;
  onPause: () => void;
  onActivate: () => void;
  onDelete: () => void;
}) {
  const meta = STATE_META[schedule.state] ?? { label: titleCase(schedule.state), intent: 'muted' as const };

  return (
    <div className="space-y-4 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium">{titleCase(schedule.interval)}</span>
        <Badge intent={meta.intent}>{meta.label}</Badge>
      </div>

      {meta.hint ? <p className="text-xs text-muted-foreground">{meta.hint}</p> : null}

      <Separator />

      <div className="space-y-1.5 text-muted-foreground">
        <div className="flex justify-between">
          <span>Next run</span>
          <span className="text-foreground">
            {schedule.state === 'active' ? formatDateTime(schedule.next_run_at) : '—'}
          </span>
        </div>
        <div className="flex justify-between">
          <span>Last run</span>
          <span className="text-foreground">
            {schedule.last_tick_at ? formatRelative(schedule.last_tick_at) : 'Never'}
          </span>
        </div>
      </div>

      {canEdit ? (
        <>
          <Separator />
          <div className="flex flex-wrap gap-2">
            {schedule.state === 'active' ? (
              <Button size="sm" variant="outline" onClick={onPause} disabled={busy}>
                <Pause className="size-4" /> Pause
              </Button>
            ) : (
              <Button size="sm" onClick={onActivate} disabled={busy}>
                <Play className="size-4" /> Activate
              </Button>
            )}
            <Button size="sm" variant="ghost" onClick={onDelete} disabled={busy}>
              <Trash2 className="size-4" /> Delete
            </Button>
          </div>
        </>
      ) : null}
    </div>
  );
}
