import {
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
];
