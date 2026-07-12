import { Compass } from 'lucide-react';
import { Link } from 'react-router-dom';
import { Button } from '@/components/ui/button';
import { EmptyState } from '@/components/common/states';

export function NotFoundPage() {
  return (
    <div className="py-16">
      <EmptyState
        icon={Compass}
        title="Page not found"
        description="That route doesn't exist. It may have moved, or it's a capability arriving in a later phase."
        action={
          <Button asChild>
            <Link to="/">Back to overview</Link>
          </Button>
        }
      />
    </div>
  );
}
