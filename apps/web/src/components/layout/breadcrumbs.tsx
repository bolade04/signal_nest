import { ChevronRight } from 'lucide-react';
import { Link, useLocation } from 'react-router-dom';
import { navItems } from './nav';
import { titleCase } from '@/lib/utils';

const staticLabels: Record<string, string> = Object.fromEntries(
  navItems.map((n) => [n.to.replace('/', '') || 'overview', n.label]),
);
staticLabels['scout-requests'] = 'Scout Requests';
staticLabels['opportunities'] = 'Opportunities';
staticLabels['context'] = 'Campaign Context';

export function Breadcrumbs() {
  const { pathname } = useLocation();
  const segments = pathname.split('/').filter(Boolean);

  const crumbs = [{ label: 'Overview', to: '/' }];
  let path = '';
  for (const segment of segments) {
    path += `/${segment}`;
    // Detail routes carry ids; show a shortened id rather than a raw uuid.
    const isId = /[0-9a-f]{8}-|^\d+$/.test(segment) || segment.length > 20;
    crumbs.push({
      label: isId ? 'Detail' : (staticLabels[segment] ?? titleCase(segment)),
      to: path,
    });
  }

  if (crumbs.length <= 1) return null;

  // The one item of the header row that gives way when space runs short (P6-UI-011):
  // it shrinks, and each label truncates on one line rather than wrapping.
  return (
    <nav aria-label="Breadcrumb" className="hidden min-w-0 md:block">
      <ol className="flex min-w-0 items-center gap-1 text-sm text-muted-foreground">
        {crumbs.map((crumb, i) => {
          const last = i === crumbs.length - 1;
          return (
            <li key={crumb.to} className="flex min-w-0 items-center gap-1">
              {i > 0 ? <ChevronRight className="size-3.5 shrink-0 opacity-60" aria-hidden /> : null}
              {last ? (
                <span aria-current="page" className="truncate font-medium text-foreground">
                  {crumb.label}
                </span>
              ) : (
                <Link to={crumb.to} className="truncate hover:text-foreground hover:underline">
                  {crumb.label}
                </Link>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
