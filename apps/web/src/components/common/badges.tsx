import { Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import {
  classificationIntent,
  classificationLabels,
  confidenceBand,
  decisionIntent,
  decisionLabels,
  jobStatusIntent,
  jobStatusLabels,
  label,
  riskIntent,
  riskLabels,
  scoutStatusIntent,
  scoutStatusLabels,
  statusIntent,
  statusLabels,
} from '@/lib/labels';

export const ClassificationBadge = ({ value }: { value: string }) => (
  <Badge intent={classificationIntent[value] ?? 'neutral'}>
    {label(classificationLabels, value)}
  </Badge>
);

export const DecisionBadge = ({ value }: { value: string }) => (
  <Badge intent={decisionIntent[value] ?? 'neutral'}>{label(decisionLabels, value)}</Badge>
);

export const RiskBadge = ({ value }: { value: string }) => (
  <Badge intent={riskIntent[value] ?? 'neutral'}>{label(riskLabels, value)}</Badge>
);

export const StatusBadge = ({ value }: { value: string }) => (
  <Badge intent={statusIntent[value] ?? 'neutral'}>{label(statusLabels, value)}</Badge>
);

export const ScoutStatusBadge = ({ value }: { value: string }) => (
  <Badge intent={scoutStatusIntent[value] ?? 'neutral'}>{label(scoutStatusLabels, value)}</Badge>
);

export const JobStatusBadge = ({ value }: { value: string }) => (
  <Badge intent={jobStatusIntent[value] ?? 'neutral'}>{label(jobStatusLabels, value)}</Badge>
);

// Same word as the confidence pill and meter: all three read `confidenceBand`.
export const ConfidenceBadge = ({ level }: { level: string }) => {
  const band = confidenceBand(level);
  return <Badge intent={band?.intent ?? 'neutral'}>{band?.label ?? label({}, level)} confidence</Badge>;
};

export const SimulatedBadge = () => (
  <Badge intent="warning" title="Generated from fixture data, not a live source">
    <Sparkles className="size-3" /> Simulated
  </Badge>
);
