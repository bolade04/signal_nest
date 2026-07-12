import { Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';

export function Spinner({ className, ...props }: React.HTMLAttributes<SVGSVGElement>) {
  return (
    <Loader2
      className={cn('animate-spin text-muted-foreground', className)}
      role="status"
      aria-label="Loading"
      {...props}
    />
  );
}
