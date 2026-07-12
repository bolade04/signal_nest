import { ArrowUpRight, MapPin } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { OpportunityCard } from '@/api/types';
import {
  ClassificationBadge,
  ConfidenceBadge,
  DecisionBadge,
  RiskBadge,
  SimulatedBadge,
  StatusBadge,
} from '@/components/common/badges';
import { Card, CardContent } from '@/components/ui/card';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { scoreBand, scoreHelp } from '@/lib/labels';
import { formatRelative } from '@/lib/utils';

function ScorePill({ label, value, help }: { label: string; value: number; help: string }) {
  const band = scoreBand(value);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="rounded-md border border-border bg-background px-2 py-1 text-center">
          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
          <p className="text-sm font-semibold tabular-nums">
            {Math.round(value)}
            <span className="ml-1 text-[10px] font-normal text-muted-foreground">{band.label}</span>
          </p>
        </div>
      </TooltipTrigger>
      <TooltipContent>{help}</TooltipContent>
    </Tooltip>
  );
}

export function OpportunityCardView({ opportunity }: { opportunity: OpportunityCard }) {
  const o = opportunity;
  return (
    <Card className="flex h-full flex-col transition-shadow hover:shadow-md">
      <CardContent className="flex flex-1 flex-col gap-3 p-5">
        <div className="flex items-start justify-between gap-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <ClassificationBadge value={o.classification} />
            <DecisionBadge value={o.decision} />
            {o.is_simulated ? <SimulatedBadge /> : null}
          </div>
          <StatusBadge value={o.status} />
        </div>

        <Link to={`/opportunities/${o.id}`} className="group">
          <h3 className="font-semibold leading-snug group-hover:text-primary">
            {o.title}
            <ArrowUpRight className="ml-1 inline size-3.5 opacity-0 transition-opacity group-hover:opacity-100" />
          </h3>
        </Link>

        {o.why_it_matters ? (
          <p className="line-clamp-2 text-sm text-muted-foreground">{o.why_it_matters}</p>
        ) : null}

        <div className="grid grid-cols-3 gap-1.5">
          <ScorePill label="Score" value={o.opportunity_score} help={scoreHelp.opportunity_score} />
          <ScorePill label="Relevance" value={o.relevance_score} help={scoreHelp.relevance_score} />
          <ScorePill label="Confidence" value={o.confidence_score} help={scoreHelp.confidence_score} />
        </div>

        <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1.5 pt-1 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            <MapPin className="size-3.5" />
            {o.resolved_market ?? 'Unspecified'}
            {!o.inside_scout_area ? (
              <span className="ml-1 rounded bg-warning/15 px-1 text-warning">outside area</span>
            ) : null}
          </span>
          <RiskBadge value={o.risk_level} />
          <ConfidenceBadge level={o.confidence_level} />
          <span>{o.source_summary.length} source{o.source_summary.length === 1 ? '' : 's'}</span>
          <span className="ml-auto">{formatRelative(o.created_at)}</span>
        </div>
      </CardContent>
    </Card>
  );
}
