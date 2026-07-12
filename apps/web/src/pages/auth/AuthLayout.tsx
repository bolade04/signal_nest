import { ShieldCheck, Sparkles, Target } from 'lucide-react';

const highlights = [
  { icon: Target, text: 'Scout real markets and surface only opportunities that matter.' },
  { icon: Sparkles, text: 'Every opportunity is scored, ranked and fully explained.' },
  { icon: ShieldCheck, text: 'Observed evidence stays separate from AI inference.' },
];

export function AuthLayout({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      <div className="hidden flex-col justify-between bg-slate-950 p-10 text-slate-100 lg:flex">
        <div className="flex items-center gap-2.5">
          <span className="flex size-9 items-center justify-center rounded-lg bg-primary text-primary-foreground">
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
          <div>
            <p className="font-semibold tracking-tight">SignalNest</p>
            <p className="text-xs text-slate-400">AI Scout · marketing intelligence</p>
          </div>
        </div>
        <div className="space-y-6">
          <h2 className="max-w-md text-2xl font-semibold leading-snug">
            Turn scattered market signals into explainable, ranked opportunities.
          </h2>
          <ul className="space-y-4">
            {highlights.map(({ icon: Icon, text }) => (
              <li key={text} className="flex items-start gap-3 text-sm text-slate-300">
                <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-md bg-white/10">
                  <Icon className="size-4" />
                </span>
                {text}
              </li>
            ))}
          </ul>
        </div>
        <p className="text-xs text-slate-500">
          Phase 1 &amp; 2 · Scouting workflow to explainable opportunities.
        </p>
      </div>

      <div className="flex items-center justify-center px-6 py-12">
        <div className="w-full max-w-sm">
          <div className="mb-8">
            <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
            <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}
