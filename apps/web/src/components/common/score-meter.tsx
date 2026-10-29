import { Info } from 'lucide-react';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { scoreBand, type Intent, type ScoreBand } from '@/lib/labels';
import { cn } from '@/lib/utils';

const barColor: Record<Intent, string> = {
  success: 'bg-success',
  info: 'bg-primary',
  warning: 'bg-warning',
  danger: 'bg-destructive',
  neutral: 'bg-secondary-foreground/60',
  muted: 'bg-muted-foreground/50',
};

export function ScoreMeter({
  label: title,
  value,
  help,
  band = true,
}: {
  label: string;
  value: number;
  help?: string;
  /**
   * true: the generic 0–100 band (`scoreBand`); false: no band word; a band: that band,
   * as for confidence, whose band the API defines (`confidenceBand`). Null means none.
   */
  band?: boolean | ScoreBand | null;
}) {
  const rounded = Math.round(value);
  const given = typeof band === 'object' && band !== null ? band : null;
  const { label: bandLabel, intent } = given ?? scoreBand(value);
  const showBand = band === true || given !== null;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-sm">
        <span className="flex items-center gap-1 font-medium text-foreground">
          {title}
          {help ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <button type="button" aria-label={`About ${title}`} className="text-muted-foreground">
                  <Info className="size-3.5" />
                </button>
              </TooltipTrigger>
              <TooltipContent>{help}</TooltipContent>
            </Tooltip>
          ) : null}
        </span>
        <span className="tabular-nums text-muted-foreground">
          {rounded}
          {showBand ? <span className="ml-1 text-xs">· {bandLabel}</span> : null}
        </span>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
        role="meter"
        aria-valuenow={rounded}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={title}
      >
        <div
          className={cn('h-full rounded-full transition-all', barColor[intent])}
          style={{ width: `${Math.max(0, Math.min(100, rounded))}%` }}
        />
      </div>
    </div>
  );
}
