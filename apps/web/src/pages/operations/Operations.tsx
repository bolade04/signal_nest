import { ShieldCheck } from 'lucide-react';
import { useState } from 'react';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/toast';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import type {
  Capability,
  CapabilityEffective,
  CapabilityOverride,
  CapabilityRegistryItem,
  DecisionSource,
} from '@/api/types';
import {
  useCapabilityEffective,
  useCapabilityOverrides,
  useCapabilityRegistry,
  useClearCapabilityOverride,
  useOperationalOverview,
  useSetCapabilityOverride,
  useTelemetryStatus,
} from './useOperations';

// Operator-facing labels for the resolver's deciding rule (§8.7): the UI must
// show *why* a capability is in its effective state, using the delivered
// DecisionSource contract vocabulary.
const decisionLabels: Record<DecisionSource, { label: string; intent: 'danger' | 'info' | 'neutral' | 'muted' }> = {
  safety_ceiling: { label: 'Safety ceiling', intent: 'danger' },
  workspace_override: { label: 'Workspace override', intent: 'info' },
  global_configuration: { label: 'Global configuration', intent: 'neutral' },
  secure_default: { label: 'Secure default', intent: 'muted' },
};

interface PendingAction {
  kind: 'enable' | 'disable' | 'clear';
  capability: Capability;
  label: string;
}

function StatusCounts({ counts }: { counts: Record<string, number> }) {
  const entries = Object.entries(counts);
  if (!entries.length) {
    return <p className="text-sm text-muted-foreground">No records yet.</p>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([status, count]) => (
        <Badge key={status} intent="neutral">
          {status.replaceAll('_', ' ')}: {count}
        </Badge>
      ))}
    </div>
  );
}

function CapabilityRow({
  item,
  effective,
  override,
  busy,
  onAction,
}: {
  item: CapabilityRegistryItem;
  effective: CapabilityEffective | undefined;
  override: CapabilityOverride | undefined;
  busy: boolean;
  onAction: (action: PendingAction) => void;
}) {
  const decided = effective ? decisionLabels[effective.decided_by] : null;
  return (
    <div className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium">{item.label}</p>
          <p className="text-xs text-muted-foreground">
            First activation planned in Phase {item.future_activation_phase}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {decided ? <Badge intent={decided.intent}>{decided.label}</Badge> : null}
          {effective ? (
            <Badge intent={effective.effective_enabled ? 'success' : 'muted'}>
              {effective.effective_enabled ? 'Enabled' : 'Disabled'}
            </Badge>
          ) : null}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>
          Global flag: {effective?.global_flag ? 'on' : 'off'}
          {/* Stored-row visibility is driven by the override ROW, not the
              resolver's has_override: a stored row the resolver refuses to
              honor (has_override=false) must still be visible and clearable. */}
          {override ? (
            <>
              {' · '}Override: {override.enabled ? 'enabled' : 'disabled'}
              {override.reason ? ` — “${override.reason}”` : ''}
              {effective && !effective.has_override
                ? ' (not honored — secure default applies)'
                : ''}
            </>
          ) : (
            <> · No workspace override (dark default applies)</>
          )}
        </span>
        <span className="flex gap-2">
          {item.workspace_enableable ? (
            <Button
              size="sm"
              variant="outline"
              aria-label={`Enable ${item.label} in this workspace`}
              disabled={busy || override?.enabled === true}
              onClick={() =>
                onAction({ kind: 'enable', capability: item.capability, label: item.label })
              }
            >
              Enable in workspace
            </Button>
          ) : (
            <Button
              size="sm"
              variant="outline"
              disabled
              aria-label={`Enable ${item.label} in this workspace (not permitted: per-workspace enablement is disallowed for this capability)`}
              title="Per-workspace enablement is not permitted for this capability"
            >
              Enable in workspace
            </Button>
          )}
          {item.workspace_disableable ? (
            <Button
              size="sm"
              variant="outline"
              aria-label={`Disable ${item.label} in this workspace`}
              disabled={busy || override?.enabled === false}
              onClick={() =>
                onAction({ kind: 'disable', capability: item.capability, label: item.label })
              }
            >
              Disable in workspace
            </Button>
          ) : null}
          {override ? (
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Clear the ${item.label} override`}
              disabled={busy}
              onClick={() =>
                onAction({ kind: 'clear', capability: item.capability, label: item.label })
              }
            >
              Clear override
            </Button>
          ) : null}
        </span>
      </div>
    </div>
  );
}

export function OperationsPage() {
  const { organizationId, workspaceId, activeWorkspace } = useWorkspace();
  const { toast } = useToast();
  const [pending, setPending] = useState<PendingAction | null>(null);

  const overview = useOperationalOverview();
  const telemetry = useTelemetryStatus();
  const registry = useCapabilityRegistry();
  const effective = useCapabilityEffective(organizationId, workspaceId);
  const overrides = useCapabilityOverrides(organizationId, workspaceId);

  const setOverride = useSetCapabilityOverride(organizationId, workspaceId);
  const clearOverride = useClearCapabilityOverride(organizationId, workspaceId);
  const mutating = setOverride.isPending || clearOverride.isPending;

  const effectiveByCapability = new Map(
    (effective.data?.items ?? []).map((item) => [item.capability, item]),
  );
  const overrideByCapability = new Map(
    (overrides.data?.items ?? []).map((item) => [item.capability, item]),
  );

  const runPending = async () => {
    if (!pending || !organizationId || !workspaceId) return;
    try {
      if (pending.kind === 'clear') {
        await clearOverride.mutateAsync(pending.capability);
        toast({ title: `Override cleared for ${pending.label}`, intent: 'success' });
      } else {
        await setOverride.mutateAsync({
          organization_id: organizationId,
          workspace_id: workspaceId,
          capability: pending.capability,
          enabled: pending.kind === 'enable',
        });
        toast({
          title: `${pending.label} override set`,
          description: `Now ${pending.kind === 'enable' ? 'enabled' : 'disabled'} for ${activeWorkspace?.name ?? 'this workspace'}.`,
          intent: 'success',
        });
      }
    } catch (err) {
      toast({
        title: 'Override change failed',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      });
    }
  };

  const capabilitiesLoading = registry.isLoading || effective.isLoading || overrides.isLoading;
  const capabilitiesError = registry.error ?? effective.error ?? overrides.error;

  return (
    <div>
      <PageHeader
        title="Operations"
        description="Operator-only observability: capability activation state, queue and worker health, and telemetry posture. Everything here ships dark."
      />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Capability activation</CardTitle>
            <Badge intent="outline">
              {activeWorkspace ? `Workspace: ${activeWorkspace.name}` : 'No workspace selected'}
            </Badge>
          </CardHeader>
          <CardContent className="space-y-2">
            {!organizationId || !workspaceId ? (
              <EmptyState
                icon={ShieldCheck}
                title="Select a workspace"
                description="Capability activation state is resolved per workspace. Pick a workspace in the switcher above to inspect or change its overrides."
              />
            ) : capabilitiesLoading ? (
              <LoadingRows rows={3} />
            ) : capabilitiesError ? (
              <ErrorState
                error={capabilitiesError}
                onRetry={() => {
                  void registry.refetch();
                  void effective.refetch();
                  void overrides.refetch();
                }}
              />
            ) : !registry.data?.items?.length ? (
              <EmptyState
                icon={ShieldCheck}
                title="No capabilities registered"
                description="The capability registry returned no entries. This should not happen on a healthy deployment."
              />
            ) : (
              <>
                {(registry.data?.items ?? []).map((item) => (
                  <CapabilityRow
                    key={item.capability}
                    item={item}
                    effective={effectiveByCapability.get(item.capability)}
                    override={overrideByCapability.get(item.capability)}
                    busy={mutating}
                    onAction={setPending}
                  />
                ))}
                <p className="pt-1 text-xs text-muted-foreground">
                  Overrides are deny-biased: the safety ceiling always wins, and clearing an
                  override returns the workspace to the global configuration or the secure
                  default. Every change is audited.
                </p>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Queue health</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {overview.isLoading ? (
              <LoadingRows rows={2} />
            ) : overview.error ? (
              <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
            ) : overview.data ? (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Total jobs</span>
                  <span className="font-medium">{overview.data.jobs.total}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Stuck jobs</span>
                  <Badge intent={overview.data.jobs.stuck_count > 0 ? 'danger' : 'success'}>
                    {overview.data.jobs.stuck_count}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Dead-lettered</span>
                  <Badge intent={overview.data.jobs.dead_letter_count > 0 ? 'danger' : 'success'}>
                    {overview.data.jobs.dead_letter_count}
                  </Badge>
                </div>
                <StatusCounts counts={overview.data.jobs.status_counts} />
              </>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Worker fleet</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {overview.isLoading ? (
              <LoadingRows rows={2} />
            ) : overview.error ? (
              <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
            ) : overview.data ? (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Active workers</span>
                  <Badge intent={overview.data.workers.active_count > 0 ? 'success' : 'muted'}>
                    {overview.data.workers.active_count}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Stale workers</span>
                  <Badge intent={overview.data.workers.stale_count > 0 ? 'warning' : 'success'}>
                    {overview.data.workers.stale_count}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Stale threshold</span>
                  <span className="font-medium">{overview.data.stale_after_seconds}s</span>
                </div>
                <StatusCounts counts={overview.data.workers.status_counts} />
              </>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Schedules</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {overview.isLoading ? (
              <LoadingRows rows={2} />
            ) : overview.error ? (
              <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
            ) : overview.data ? (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Total schedules</span>
                  <span className="font-medium">{overview.data.schedules.total}</span>
                </div>
                <StatusCounts counts={overview.data.schedules.state_counts} />
              </>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Telemetry posture</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {telemetry.isLoading ? (
              <LoadingRows rows={2} />
            ) : telemetry.error ? (
              <ErrorState error={telemetry.error} onRetry={() => void telemetry.refetch()} />
            ) : telemetry.data ? (
              <>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Logging format</span>
                  <span className="font-medium">{telemetry.data.logging_format}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Metrics</span>
                  <Badge intent={telemetry.data.metrics_enabled ? 'success' : 'muted'}>
                    {telemetry.data.metrics_enabled
                      ? telemetry.data.exporter_status
                      : 'disabled'}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Tracing</span>
                  <Badge intent={telemetry.data.tracing_enabled ? 'success' : 'muted'}>
                    {telemetry.data.tracing_enabled ? telemetry.data.tracing_status : 'disabled'}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Telemetry failures</span>
                  <Badge intent={telemetry.data.telemetry_failures > 0 ? 'danger' : 'success'}>
                    {telemetry.data.telemetry_failures}
                  </Badge>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Redaction</span>
                  <Badge intent={telemetry.data.redaction_enabled ? 'success' : 'danger'}>
                    {telemetry.data.redaction_enabled ? 'enabled' : 'disabled'}
                  </Badge>
                </div>
              </>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) setPending(null);
        }}
        title={
          pending?.kind === 'clear'
            ? `Clear the ${pending.label} override?`
            : `${pending?.kind === 'enable' ? 'Enable' : 'Disable'} ${pending?.label ?? ''} in this workspace?`
        }
        description={
          pending?.kind === 'clear'
            ? 'The workspace returns to the global configuration or the secure default. This change is audited.'
            : pending?.kind === 'enable'
              ? `Sets a workspace override enabling ${pending.label} for ${activeWorkspace?.name ?? 'the selected workspace'} only, subject to the safety ceiling. No other workspace is affected. This change is audited.`
              : `Sets a workspace override disabling ${pending?.label ?? 'this capability'} for ${activeWorkspace?.name ?? 'the selected workspace'} regardless of the global flag. This change is audited.`
        }
        confirmLabel={pending?.kind === 'clear' ? 'Clear override' : 'Confirm'}
        destructive={pending?.kind === 'clear' || pending?.kind === 'disable'}
        onConfirm={runPending}
      />
    </div>
  );
}
