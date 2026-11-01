import {
  Activity,
  ClipboardCheck,
  Compass,
  LayoutDashboard,
  ListChecks,
  MapPin,
  Settings,
  Sparkles,
  Target,
} from 'lucide-react';

export interface NavItem {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  end?: boolean;
  description: string;
  /**
   * Shown only when the server-authoritative session says the user is an
   * operator. The entry stays in this array regardless so breadcrumb labels
   * (derived at module scope) remain stable; the sidebar filters at render time.
   */
  operatorOnly?: boolean;
  /**
   * The approval queue (Phase 5B, mounted at I-5B): shown only while the member's role in
   * the active organization is exactly `reviewer` AND recommendation briefs resolve enabled
   * for the active workspace (`useApprovalQueueVisible`). Like `operatorOnly`, the entry
   * stays in this array so the breadcrumb label is stable; the sidebar filters at render
   * time. UX only — the route renders the not-found page while the workspace is dark, and
   * every approval endpoint re-decides role and capability on the server.
   */
  approverOnly?: boolean;
}

export const navItems: NavItem[] = [
  {
    to: '/',
    label: 'Overview',
    icon: LayoutDashboard,
    end: true,
    description: 'Scouting health at a glance',
  },
  {
    to: '/onboarding',
    label: 'Onboarding',
    icon: Compass,
    description: 'Set up your business profile',
  },
  {
    to: '/context',
    label: 'Campaign Context',
    icon: Target,
    description: 'Products, audiences, claims and rules',
  },
  {
    to: '/scout-requests',
    label: 'Scout Requests',
    icon: ListChecks,
    description: 'Run and manage market scouts',
  },
  {
    to: '/opportunities',
    label: 'Opportunities',
    icon: Sparkles,
    description: 'Explainable, scored opportunities',
  },
  {
    to: '/approvals',
    label: 'Approvals',
    icon: ClipboardCheck,
    description: 'Recommendation briefs awaiting a reviewer decision',
    approverOnly: true,
  },
  {
    to: '/locations',
    label: 'Locations',
    icon: MapPin,
    description: 'Markets and service areas',
  },
  {
    to: '/settings',
    label: 'Settings',
    icon: Settings,
    description: 'Workspace and account settings',
  },
  {
    to: '/operations',
    label: 'Operations',
    icon: Activity,
    description: 'Operator observability and capability control',
    operatorOnly: true,
  },
];
