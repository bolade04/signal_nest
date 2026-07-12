import { Sparkles } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import {
  classificationIntent,
  classificationLabels,
  confidenceIntent,
  decisionIntent,
  decisionLabels,
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

export const ConfidenceBadge = ({ level }: { level: string }) => (
  <Badge intent={confidenceIntent[level] ?? 'neutral'}>{label({}, level)} confidence</Badge>
);

export const SimulatedBadge = () => (
  <Badge intent="warning" title="Generated from fixture data, not a live source">
    <Sparkles className="size-3" /> Simulated
  </Badge>
);
