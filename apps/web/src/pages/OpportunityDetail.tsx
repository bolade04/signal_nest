import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowLeft,
  Bookmark,
  BrainCircuit,
  CheckCircle2,
  Eye,
  EyeOff,
  ExternalLink,
  Lightbulb,
  MapPin,
  ShieldAlert,
  Target,
} from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OpportunityDetail } from '@/api/types';
import {
  ClassificationBadge,
  ConfidenceBadge,
  DecisionBadge,
  RiskBadge,
  SimulatedBadge,
  StatusBadge,
} from '@/components/common/badges';
import { ScoreMeter } from '@/components/common/score-meter';
import { ErrorState, LoadingRows } from '@/components/common/states';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { useToast } from '@/components/ui/toast';
import { scoreHelp } from '@/lib/labels';
import { formatDateTime, titleCase } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { OpportunityIntelligencePanel } from './opportunities/IntelligencePanel';

function str(value: unknown): string | null {
  if (value == null) return null;
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return null;
}

function EvidenceItem({ item }: { item: Record<string, unknown> }) {
  const excerpt = str(item.excerpt) ?? str(item.content) ?? str(item.detail) ?? str(item.text);
  const sourceType = str(item.source_type) ?? str(item.source);
  const author = str(item.author);
  const timestamp = str(item.timestamp) ?? str(item.created_at);
  const url = str(item.source_url) ?? str(item.url);

  return (
    <li className="rounded-md border border-border bg-background p-3">
      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {sourceType ? <Badge intent="outline">{titleCase(sourceType)}</Badge> : null}
        {author ? <span>{author}</span> : null}
        {timestamp ? <span>· {formatDateTime(timestamp)}</span> : null}
      </div>
      {excerpt ? <p className="text-sm text-foreground">{excerpt}</p> : null}
      {url ? (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="mt-1 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
        >
          View source <ExternalLink className="size-3" />
        </a>
      ) : null}
    </li>
  );
}

function DetailInner({ workspaceId, opportunityId }: { workspaceId: string; opportunityId: string }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const navigate = useNavigate();

  const query = useQuery({
    queryKey: queryKeys.opportunity(workspaceId, opportunityId),
    queryFn: ({ signal }) => api.getOpportunity(workspaceId, opportunityId, signal),
  });

  const statusMutation = useMutation({
    mutationFn: (status: string) => api.updateOpportunityStatus(workspaceId, opportunityId, status),
    onSuccess: async (card) => {
      queryClient.setQueryData<OpportunityDetail | undefined>(
        queryKeys.opportunity(workspaceId, opportunityId),
        (prev) => (prev ? { ...prev, status: card.status } : prev),
      );
      await queryClient.invalidateQueries({
        queryKey: ['workspaces', workspaceId, 'opportunities'],
      });
      toast({ title: `Marked as ${card.status}`, intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not update status',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });

  if (query.isLoading) return <LoadingRows rows={5} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  if (!query.data) return null;

  const o = query.data;

  const statusActions: { status: string; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { status: 'saved', label: 'Save', icon: Bookmark },
    { status: 'monitoring', label: 'Monitor', icon: Eye },
    { status: 'actioned', label: 'Mark actioned', icon: CheckCircle2 },
    { status: 'ignored', label: 'Ignore', icon: EyeOff },
  ];

  return (
    <div className="space-y-6">
      <div>
        <Button variant="ghost" size="sm" onClick={() => navigate(-1)} className="mb-3 -ml-2">
          <ArrowLeft className="size-4" /> Back
        </Button>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <ClassificationBadge value={o.classification} />
              <DecisionBadge value={o.decision} />
              <RiskBadge value={o.risk_level} />
              <ConfidenceBadge level={o.confidence_level} />
              <StatusBadge value={o.status} />
              {o.is_simulated ? <SimulatedBadge /> : null}
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">{o.title}</h1>
            <p className="mt-1 flex items-center gap-2 text-sm text-muted-foreground">
              <MapPin className="size-4" />
              {o.resolved_market ?? 'Unspecified market'}
              {o.inside_scout_area ? (
                <Badge intent="success">Inside scout area</Badge>
              ) : (
                <Badge intent="warning">Outside scout area</Badge>
              )}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            {statusActions.map((action) => {
              const Icon = action.icon;
              const active = o.status === action.status;
              return (
                <Button
                  key={action.status}
                  variant={active ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => statusMutation.mutate(action.status)}
                  disabled={statusMutation.isPending}
                  aria-pressed={active}
                >
                  <Icon className="size-4" /> {action.label}
                </Button>
              );
            })}
          </div>
        </div>
      </div>

      {o.is_simulated ? (
        <div className="rounded-md border border-warning/40 bg-warning/10 px-4 py-2.5 text-sm text-warning-foreground">
          <span className="font-medium text-warning">Simulated opportunity.</span>{' '}
          <span className="text-muted-foreground">
            Generated from fixture connectors for demonstration — not sourced from a live feed.
          </span>
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          {/* Why it matters */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Target className="size-4 text-primary" /> Why this matters
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {o.why_it_matters ? <p>{o.why_it_matters}</p> : null}
              {o.who_cares ? (
                <p className="text-muted-foreground">
                  <span className="font-medium text-foreground">Who cares: </span>
                  {o.who_cares}
                </p>
              ) : null}
              <div className="flex flex-wrap gap-4 text-sm text-muted-foreground">
                {o.audience_fit ? (
                  <span>
                    <span className="font-medium text-foreground">Audience fit:</span> {o.audience_fit}
                  </span>
                ) : null}
                {o.urgency ? (
                  <span>
                    <span className="font-medium text-foreground">Urgency:</span> {o.urgency}
                  </span>
                ) : null}
                {o.commercial_value ? (
                  <span>
                    <span className="font-medium text-foreground">Commercial value:</span>{' '}
                    {o.commercial_value}
                  </span>
                ) : null}
              </div>
            </CardContent>
          </Card>

          {/* Observed evidence — verifiable */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Eye className="size-4 text-primary" /> Observed evidence
              </CardTitle>
              <p className="text-sm text-muted-foreground">
                Directly collected from sources. These are facts, not interpretation.
              </p>
            </CardHeader>
            <CardContent>
              {o.observed_evidence.length ? (
                <ul className="space-y-2">
                  {o.observed_evidence.map((item, i) => (
                    <EvidenceItem key={i} item={item} />
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">No observed evidence recorded.</p>
              )}
            </CardContent>
          </Card>

          {/* AI inference — clearly separated */}
          <Card className="border-primary/30">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BrainCircuit className="size-4 text-primary" /> AI inference
              </CardTitle>
              <p className="text-sm text-muted-foreground">
                SignalNest's interpretation of the evidence above — reasoning, not verified fact.
              </p>
            </CardHeader>
            <CardContent className="space-y-3 text-sm">
              {o.ai_inference ? (
                <p>{o.ai_inference}</p>
              ) : (
                <p className="text-muted-foreground">No inference generated.</p>
              )}
              {o.suggested_angles.length ? (
                <div>
                  <p className="mb-1.5 flex items-center gap-1.5 font-medium">
                    <Lightbulb className="size-4 text-warning" /> Suggested creative angles
                  </p>
                  <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                    {o.suggested_angles.map((angle, i) => (
                      <li key={i}>{angle}</li>
                    ))}
                  </ul>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Creative generation from these angles arrives in Phase 3.
                  </p>
                </div>
              ) : null}
            </CardContent>
          </Card>

          {/* Recommended action */}
          {o.recommended_action ? (
            <Card>
              <CardHeader>
                <CardTitle>Recommended action</CardTitle>
              </CardHeader>
              <CardContent className="text-sm">{o.recommended_action}</CardContent>
            </Card>
          ) : null}

          {/* Warnings */}
          {o.claims_warnings.length || o.risk_note ? (
            <Card className="border-warning/40 bg-warning/5">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-warning">
                  <ShieldAlert className="size-4" /> Claim safety &amp; risk
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                {o.risk_note ? <p>{o.risk_note}</p> : null}
                {o.claims_warnings.length ? (
                  <ul className="list-inside list-disc space-y-1 text-muted-foreground">
                    {o.claims_warnings.map((w, i) => (
                      <li key={i}>{w}</li>
                    ))}
                  </ul>
                ) : null}
              </CardContent>
            </Card>
          ) : null}

          {/* Persisted signal intelligence (Batch 4B read-only) */}
          <OpportunityIntelligencePanel workspaceId={workspaceId} opportunityId={opportunityId} />
        </div>

        {/* Sidebar: scores, sources, validation */}
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Scores</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <ScoreMeter label="Opportunity" value={o.opportunity_score} help={scoreHelp.opportunity_score} />
              <ScoreMeter label="Relevance" value={o.relevance_score} help={scoreHelp.relevance_score} />
              <ScoreMeter label="Confidence" value={o.confidence_score} help={scoreHelp.confidence_score} />
              <ScoreMeter label="Priority" value={o.priority_score} help={scoreHelp.priority_score} band={false} />

              {(o.scores ?? []).map((breakdown) => (
                <div key={breakdown.kind} className="rounded-md border border-border p-3">
                  <p className="mb-2 text-sm font-medium">
                    {titleCase(breakdown.kind)} · {Math.round(breakdown.total)}
                  </p>
                  <dl className="space-y-1">
                    {Object.entries(breakdown.breakdown).map(([key, value]) => (
                      <div key={key} className="flex justify-between text-xs text-muted-foreground">
                        <dt>{titleCase(key)}</dt>
                        <dd className="tabular-nums text-foreground">{str(value) ?? '—'}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ))}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Sources &amp; validation</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {o.source_summary.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {o.source_summary.map((s, i) => (
                    <Badge key={i} intent="outline">
                      {titleCase(s)}
                    </Badge>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No sources listed.</p>
              )}

              {(o.validation_evidence ?? []).length ? (
                <>
                  <Separator />
                  <ul className="space-y-2">
                    {o.validation_evidence!.map((ev, i) => (
                      <li key={i} className="text-sm">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium">{titleCase(ev.source_type)}</span>
                          <Badge intent="neutral">weight {ev.weight}</Badge>
                        </div>
                        <p className="text-xs text-muted-foreground">{ev.detail}</p>
                        {ev.source_url ? (
                          <a
                            href={ev.source_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                          >
                            Source <ExternalLink className="size-3" />
                          </a>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Context</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">Scout request</span>
                <Link className="font-medium text-primary hover:underline" to={`/scout-requests/${o.scout_request_id}`}>
                  Open
                </Link>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">Created</span>
                <span>{formatDateTime(o.created_at)}</span>
              </div>
            </CardContent>
          </Card>

          <p className="px-1 text-xs text-muted-foreground">
            Known limitation: AI inference is model-generated reasoning and may be incomplete. Always
            confirm against the observed evidence and source links before acting.
          </p>
        </div>
      </div>
    </div>
  );
}

export function OpportunityDetailPage() {
  const { opportunityId } = useParams<{ opportunityId: string }>();
  const { workspaceId } = useWorkspace();
  if (!opportunityId || !workspaceId) return null;
  return (
    <RequireWorkspace>
      {({ workspaceId: ws }) => <DetailInner workspaceId={ws} opportunityId={opportunityId} />}
    </RequireWorkspace>
  );
}
