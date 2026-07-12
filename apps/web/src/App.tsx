import { Route, Routes } from 'react-router-dom';
import { AppShell } from '@/components/layout/app-shell';
import { ProtectedRoute } from '@/auth/ProtectedRoute';
import { SignInPage } from '@/pages/auth/SignIn';
import { RegisterPage } from '@/pages/auth/Register';
import { OverviewPage } from '@/pages/Overview';
import { OnboardingPage } from '@/pages/Onboarding';
import { CampaignContextPage } from '@/pages/CampaignContext';
import { LocationsPage } from '@/pages/Locations';
import { ScoutRequestsPage } from '@/pages/ScoutRequests';
import { ScoutRequestDetailPage } from '@/pages/ScoutRequestDetail';
import { OpportunitiesPage } from '@/pages/Opportunities';
import { OpportunityDetailPage } from '@/pages/OpportunityDetail';
import { SettingsPage } from '@/pages/Settings';
import { NotFoundPage } from '@/pages/NotFound';

export function App() {
  return (
    <Routes>
      <Route path="/sign-in" element={<SignInPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route element={<ProtectedRoute />}>
        <Route element={<AppShell />}>
          <Route index element={<OverviewPage />} />
          <Route path="onboarding" element={<OnboardingPage />} />
          <Route path="context" element={<CampaignContextPage />} />
          <Route path="locations" element={<LocationsPage />} />
          <Route path="scout-requests" element={<ScoutRequestsPage />} />
          <Route path="scout-requests/:requestId" element={<ScoutRequestDetailPage />} />
          <Route path="opportunities" element={<OpportunitiesPage />} />
          <Route path="opportunities/:opportunityId" element={<OpportunityDetailPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
