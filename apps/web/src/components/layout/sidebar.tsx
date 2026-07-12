import { NavLink } from 'react-router-dom';
import { navItems } from './nav';
import { cn } from '@/lib/utils';

function BrandMark() {
  return (
    <div className="flex items-center gap-2.5 px-2">
      <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
        <svg viewBox="0 0 32 32" className="size-5" aria-hidden>
          <path
            d="M16 5l9 5v12l-9 5-9-5V10l9-5z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinejoin="round"
          />
          <circle cx="16" cy="15" r="3.4" fill="currentColor" />
        </svg>
      </span>
      <div className="leading-tight">
        <p className="text-sm font-semibold tracking-tight">SignalNest</p>
        <p className="text-[11px] text-muted-foreground">AI Scout</p>
      </div>
    </div>
  );
}

export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav className="flex h-full flex-col gap-1" aria-label="Primary">
      <div className="mb-4 pt-1">
        <BrandMark />
      </div>
      <ul className="flex flex-1 flex-col gap-1">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <li key={item.to}>
              <NavLink
                to={item.to}
                end={item.end}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    'group flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    isActive
                      ? 'bg-primary/12 text-primary'
                      : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  )
                }
              >
                <Icon className="size-4 shrink-0" />
                <span className="truncate">{item.label}</span>
              </NavLink>
            </li>
          );
        })}
      </ul>
      <div className="rounded-md border border-border bg-muted/50 p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">Phase 1 &amp; 2</p>
        <p className="mt-0.5">
          Scouting to explainable opportunities. Creative generation arrives in Phase 3.
        </p>
      </div>
    </nav>
  );
}
