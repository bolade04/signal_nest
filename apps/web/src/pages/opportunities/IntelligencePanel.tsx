import { BrainCircuit, ChevronDown, Eye, Gauge, ScrollText, Sparkles } from 'lucide-react';
import { useState } from 'react';
import type {
  InferredAttribute,
  IntelligenceEvidenceItem,
  IntelligenceFacts,
  IntelligenceInference,
  IntelligencePayload,
  IntelligenceRelevance,
  IntelligenceScoreBreakdown,
} from '@/api/types';
import { ScoreMeter } from '@/components/common/score-meter';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { formatDateTime, titleCase } from '@/lib/utils';
import { useOpportunityIntelligence } from './useOpportunityIntelligence';

// Number of evidence excerpts shown before the "Show more" disclosure.
const EVIDENCE_PREVIEW = 3;

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(100, Math.round(value)));
}

/** InferredAttribute.confidence is 0..1; present as a bounded whole percentage. */
function confidencePercent(confidence: number): number {
  if (!Number.isFinite(confidence)) return 0;
  return clampPercent(confidence * 100);
}

function AttributeRow({ label, attr }: { label: string; attr: InferredAttribute | null | undefined }) {
  if (!attr) return null;
  return (
    <div className="rounded-md border border-border bg-background p-3">
      <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium text-foreground">{label}</span>
        <span className="tabular-nums text-xs text-muted-foreground">
          {confidencePercent(attr.confidence)}% confidence
        </span>
      </div>
      <p className="text-sm text-foreground">{titleCase(attr.value)}</p>
      {attr.method ? (
        <p className="mt-1 text-xs text-muted-foreground">Method: {titleCase(attr.method)}</p>
      ) : null}
    </div>
  );
}

function FactsSection({ facts }: { facts: IntelligenceFacts }) {
  const rows: [string, string][] = [
    ['Source type', titleCase(facts.source_type)],
    ...(facts.market ? ([['Market', facts.market]] as [string, string][]) : []),
    ['Language', facts.language || '—'],
    ['Published', `${Math.round(facts.published_days_ago)} day(s) ago`],
    ['Characters', String(facts.char_count)],
    ['Words', String(facts.word_count)],
    ['Distinct source types', String(facts.distinct_source_types)],
    ['Duplicates', String(facts.duplicate_count)],
    ['Engagement', String(facts.engagement)],
  ];
  return (
    <section aria-labelledby="intel-facts-heading" className="space-y-2">
      <h3 id="intel-facts-heading" className="flex items-center gap-1.5 text-sm font-semibold">
        <Eye className="size-4 text-primary" /> Observed facts
      </h3>
      <p className="text-xs text-muted-foreground">
        Directly measured from the source. Nothing here is interpreted.
      </p>
      {facts.excerpt ? (
        <p className="whitespace-pre-wrap break-words rounded-md border border-border bg-background p-3 text-sm text-foreground">
          {facts.excerpt}
        </p>
      ) : null}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-3">
        {rows.map(([k, v]) => (
          <div key={k} className="flex flex-col">
            <dt className="text-xs text-muted-foreground">{k}</dt>
            <dd className="break-words text-sm tabular-nums text-foreground">{v}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function InferenceSection({ inference }: { inference: IntelligenceInference }) {
  const flags: string[] = [];
  if (inference.has_buying_intent) flags.push('Buying intent');
  if (inference.has_competitor_dissatisfaction) flags.push('Competitor dissatisfaction');
  const hasAny =
    inference.signal_type || inference.pain_point_dna || inference.sentiment || flags.length;
  return (
    <section aria-labelledby="intel-inference-heading" className="space-y-2">
      <h3 id="intel-inference-heading" className="flex items-center gap-1.5 text-sm font-semibold">
        <BrainCircuit className="size-4 text-primary" /> Interpretation
      </h3>
      <p className="text-xs text-muted-foreground">
        Model-generated reasoning about the facts above — not verified truth.
      </p>
      {hasAny ? (
        <div className="space-y-2">
          <AttributeRow label="Signal type" attr={inference.signal_type} />
          <AttributeRow label="Pain-point DNA" attr={inference.pain_point_dna} />
          <AttributeRow label="Sentiment" attr={inference.sentiment} />
          {flags.length ? (
            <div className="flex flex-wrap gap-1.5">
              {flags.map((f) => (
                <Badge key={f} intent="info">
                  {f}
                </Badge>
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No interpretation was inferred.</p>
      )}
    </section>
  );
}

function RelevanceSection({ relevance }: { relevance: IntelligenceRelevance }) {
  const hitGroups: [string, string[]][] = [
    ['Keywords', relevance.keyword_hits],
    ['Pain points', relevance.pain_point_hits],
    ['Audience', relevance.audience_hits],
    ['Competitors', relevance.competitor_hits],
  ];
  return (
    <section aria-labelledby="intel-relevance-heading" className="space-y-2">
      <h3 id="intel-relevance-heading" className="text-sm font-semibold">
        Relevance
      </h3>
      <ScoreMeter label="Relevance score" value={clampPercent(relevance.score)} band={false} />
      {relevance.below_action_floor ? (
        <Badge intent="warning">Below action floor</Badge>
      ) : null}
      <div className="space-y-1.5">
        {hitGroups
          .filter(([, hits]) => hits.length > 0)
          .map(([label, hits]) => (
            <div key={label} className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-muted-foreground">{label}:</span>
              {hits.map((h, i) => (
                <Badge key={`${label}-${i}`} intent="outline">
                  {h}
                </Badge>
              ))}
            </div>
          ))}
      </div>
    </section>
  );
}

function ScoreSection({ score }: { score: IntelligenceScoreBreakdown }) {
  const factors = Object.entries(score.factors ?? {});
  return (
    <section aria-labelledby="intel-score-heading" className="space-y-2">
      <h3 id="intel-score-heading" className="flex items-center gap-1.5 text-sm font-semibold">
        <Gauge className="size-4 text-primary" /> Score breakdown
      </h3>
      <ScoreMeter label={`Total · ${titleCase(score.classification)}`} value={clampPercent(score.total)} band={false} />
      {factors.length ? (
        <dl className="space-y-1">
          {factors.map(([name, factor]) => (
            <div key={name} className="flex items-center justify-between text-xs">
              <dt className="text-muted-foreground">{titleCase(name)}</dt>
              <dd className="tabular-nums text-foreground">
                {factor.points.toFixed(1)} pts
                <span className="ml-1 text-muted-foreground">
                  (w {factor.weight}, v {factor.value})
                </span>
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </section>
  );
}

function EvidenceSection({ evidence }: { evidence: IntelligenceEvidenceItem[] }) {
  const [expanded, setExpanded] = useState(false);
  if (!evidence.length) return null;
  const visible = expanded ? evidence : evidence.slice(0, EVIDENCE_PREVIEW);
  const hidden = evidence.length - visible.length;
  return (
    <section aria-labelledby="intel-evidence-heading" className="space-y-2">
      <h3 id="intel-evidence-heading" className="flex items-center gap-1.5 text-sm font-semibold">
        <ScrollText className="size-4 text-primary" /> Evidence
      </h3>
      <ul className="space-y-2">
        {visible.map((item, i) => (
          <li key={i} className="rounded-md border border-border bg-background p-3">
            <p className="whitespace-pre-wrap break-words text-sm text-foreground">{item.quote}</p>
            {item.method ? (
              <p className="mt-1 text-xs text-muted-foreground">{titleCase(item.method)}</p>
            ) : null}
          </li>
        ))}
      </ul>
      {evidence.length > EVIDENCE_PREVIEW ? (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          {expanded ? 'Show fewer' : `Show ${hidden} more`}
        </Button>
      ) : null}
    </section>
  );
}

function ProvenanceDetails({ payload }: { payload: IntelligencePayload }) {
  const [open, setOpen] = useState(false);
  return (
    <section className="space-y-2">
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="-ml-2"
      >
        <ChevronDown className={open ? 'size-4 rotate-180 transition-transform' : 'size-4 transition-transform'} />
        Provenance &amp; version
      </Button>
      {open ? (
        <dl className="space-y-1 px-1 text-xs">
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Enricher</dt>
            <dd className="break-words text-foreground">{payload.provenance.enricher}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Analysis version</dt>
            <dd className="break-words text-foreground">{payload.version.analysis_version}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Scoring version</dt>
            <dd className="break-words text-foreground">{payload.version.scoring_version}</dd>
          </div>
          <div className="flex justify-between gap-2">
            <dt className="text-muted-foreground">Created</dt>
            <dd className="text-foreground">{formatDateTime(payload.created_at)}</dd>
          </div>
        </dl>
      ) : null}
    </section>
  );
}

function IntelligenceSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading intelligence…</span>
      <Skeleton className="h-5 w-40" />
      <Skeleton className="h-16 w-full" />
      <Skeleton className="h-16 w-full" />
    </div>
  );
}

function IntelligenceBody({ payload }: { payload: IntelligencePayload }) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge intent="info">{titleCase(payload.classification)}</Badge>
        {payload.decision ? <Badge intent="neutral">{titleCase(payload.decision)}</Badge> : null}
        {payload.is_simulated ? <Badge intent="warning">Simulated</Badge> : null}
      </div>
      {payload.rationale ? (
        <p className="whitespace-pre-wrap break-words text-sm text-foreground">{payload.rationale}</p>
      ) : null}
      <Separator />
      <RelevanceSection relevance={payload.relevance} />
      <Separator />
      <FactsSection facts={payload.facts} />
      <Separator />
      <InferenceSection inference={payload.inference} />
      <EvidenceSection evidence={payload.evidence} />
      <Separator />
      <ScoreSection score={payload.score} />
      <Separator />
      <ProvenanceDetails payload={payload} />
    </div>
  );
}

export function OpportunityIntelligencePanel({
  workspaceId,
  opportunityId,
}: {
  workspaceId: string;
  opportunityId: string;
}) {
  const query = useOpportunityIntelligence({ workspaceId, opportunityId });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="size-4 text-primary" /> Signal intelligence
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          Persisted analysis for this opportunity — observed facts kept separate from interpretation.
        </p>
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <IntelligenceSkeleton />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        ) : query.data?.intelligence ? (
          <IntelligenceBody payload={query.data.intelligence} />
        ) : (
          <p className="text-sm text-muted-foreground">
            No intelligence analysis is available for this opportunity yet.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
