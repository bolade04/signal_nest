import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, ChevronLeft, ChevronRight, Rocket } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { BusinessProfileBase, OnboardingRequest } from '@/api/types';
import { Field } from '@/components/common/form-field';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { sourceTypeLabels } from '@/lib/labels';
import { cn } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// Every presence path is optional — a brand-new business with no online presence
// is a first-class path. Wording stays friendly and inclusive.
const PRESENCE_PATHS: { value: string; title: string; description: string }[] = [
  { value: 'website', title: 'A website', description: 'You have a site customers can visit.' },
  { value: 'social', title: 'Social profiles', description: 'You reach people on social platforms.' },
  { value: 'website_social', title: 'Website + social', description: 'Both a site and social presence.' },
  { value: 'google_business', title: 'Google Business Profile', description: 'You show up on Google Maps / Search.' },
  { value: 'marketplace', title: 'A marketplace', description: 'You sell via Amazon, Etsy, an app store, etc.' },
  { value: 'offline', title: 'Mostly offline', description: 'Word of mouth, foot traffic, local presence.' },
  { value: 'none', title: 'Brand new', description: 'No presence yet — starting from scratch.' },
];

const SOCIAL_PLATFORMS = ['instagram', 'facebook', 'tiktok', 'linkedin', 'x', 'youtube'];

interface OnboardingForm {
  brand_name: string;
  profile: BusinessProfileBase;
  preferred_source_types: string[];
}

function emptyForm(): OnboardingForm {
  return {
    brand_name: '',
    preferred_source_types: ['manual', 'website_scan', 'rss_news'],
    profile: {
      company_name: '',
      industry: null,
      business_type: null,
      website: null,
      alternative_presence: null,
      social_links: {},
      google_business_profile: null,
      marketplace_links: [],
      description: null,
      core_problem_solved: null,
      unique_value_proposition: null,
      target_audience: null,
      ideal_customer_profile: null,
      markets_served: [],
      customer_pain_points: [],
      common_objections: [],
      pricing_model: null,
      campaign_goals: [],
      preferred_platforms: [],
      onboarding_path: null,
    },
  };
}

const STEPS = ['Presence', 'Business', 'Reach', 'Value & audience', 'Sources', 'Review'] as const;

function WizardInner({ workspaceId }: { workspaceId: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { organizationId } = useWorkspace();

  const draftKey = `signalnest-onboarding-${workspaceId}`;
  const [form, setForm] = useState<OnboardingForm>(() => {
    const raw = localStorage.getItem(draftKey);
    if (raw) {
      try {
        return { ...emptyForm(), ...(JSON.parse(raw) as OnboardingForm) };
      } catch {
        /* fall through */
      }
    }
    return emptyForm();
  });
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Seed the wizard with any profile the workspace already has, so returning
  // users resume from real saved data rather than a blank slate.
  const existing = useQuery({
    queryKey: queryKeys.businessProfile(workspaceId),
    queryFn: ({ signal }) => api.getBusinessProfile(workspaceId, signal),
    retry: false,
  });
  // Seed the wizard from the saved profile on the first render only, matching the
  // original effect which fired before the autosave below wrote a draft: only when
  // the profile is already cached and the user has no saved draft. Done as a
  // one-shot render-phase adjustment instead of a setState-in-effect.
  const [hadInitialDraft] = useState(() => localStorage.getItem(draftKey) != null);
  const [seedChecked, setSeedChecked] = useState(false);
  if (!seedChecked) {
    setSeedChecked(true);
    if (existing.data && !hadInitialDraft) {
      const data = existing.data;
      setForm((f) => ({
        ...f,
        brand_name: f.brand_name || data.company_name,
        profile: { ...f.profile, ...data },
      }));
    }
  }

  // Autosave the draft as the user edits so progress is never lost on reload.
  useEffect(() => {
    localStorage.setItem(draftKey, JSON.stringify(form));
  }, [draftKey, form]);

  const setProfile = <K extends keyof BusinessProfileBase>(key: K, value: BusinessProfileBase[K]) =>
    setForm((f) => ({ ...f, profile: { ...f.profile, [key]: value } }));

  const setSocial = (platform: string, value: string) =>
    setForm((f) => {
      const links = { ...(f.profile.social_links ?? {}) };
      if (value.trim()) links[platform] = value;
      else delete links[platform];
      return { ...f, profile: { ...f.profile, social_links: links } };
    });

  const nameValue = form.brand_name.trim() || (form.profile.company_name ?? '').trim();
  const canSubmit = nameValue.length > 0;

  const submit = useMutation({
    mutationFn: () => {
      const brand = form.brand_name.trim() || form.profile.company_name;
      const payload: OnboardingRequest = {
        brand_name: brand,
        profile: { ...form.profile, company_name: form.profile.company_name || brand },
        preferred_source_types: form.preferred_source_types,
      };
      return api.onboard(workspaceId, payload);
    },
    onSuccess: async () => {
      localStorage.removeItem(draftKey);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.businessProfile(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.brands(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.workspace(workspaceId) }),
        organizationId
          ? queryClient.invalidateQueries({ queryKey: queryKeys.workspaces(organizationId) })
          : Promise.resolve(),
      ]);
      toast({ title: 'Onboarding complete', description: 'Your workspace is ready to scout.', intent: 'success' });
      navigate('/');
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not complete onboarding.'),
  });

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const prev = () => setStep((s) => Math.max(s - 1, 0));
  const stepValid = step !== 1 || canSubmit; // Business step needs a name.

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader
        title="Guided onboarding"
        description="Tell SignalNest about your business. Every field is optional except a name — you can refine everything later in Campaign Context."
      />

      {/* Progress */}
      <ol className="mb-6 flex flex-wrap gap-2" aria-label="Onboarding progress">
        {STEPS.map((s, i) => {
          const done = i < step;
          const active = i === step;
          return (
            <li key={s}>
              <button
                type="button"
                onClick={() => setStep(i)}
                aria-current={active ? 'step' : undefined}
                className={cn(
                  'flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                  active
                    ? 'border-primary bg-primary text-primary-foreground'
                    : done
                      ? 'border-primary/40 bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:bg-secondary',
                )}
              >
                <span className="flex size-4 items-center justify-center rounded-full border border-current text-[10px]">
                  {done ? <Check className="size-3" /> : i + 1}
                </span>
                {s}
              </button>
            </li>
          );
        })}
      </ol>

      {error ? (
        <div role="alert" className="mb-4 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <div className="rounded-lg border border-border bg-card p-6">
        {step === 0 ? (
          <fieldset>
            <legend className="mb-1 text-lg font-semibold">How do customers find you today?</legend>
            <p className="mb-4 text-sm text-muted-foreground">
              Pick the closest match. This just tailors the next questions — nothing is locked in.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {PRESENCE_PATHS.map((p) => {
                const selected = form.profile.onboarding_path === p.value;
                return (
                  <button
                    key={p.value}
                    type="button"
                    onClick={() => setProfile('onboarding_path', p.value)}
                    aria-pressed={selected}
                    className={cn(
                      'rounded-lg border p-4 text-left transition-colors',
                      selected ? 'border-primary bg-primary/5 ring-1 ring-primary' : 'border-border hover:bg-secondary/50',
                    )}
                  >
                    <p className="font-medium">{p.title}</p>
                    <p className="mt-0.5 text-sm text-muted-foreground">{p.description}</p>
                  </button>
                );
              })}
            </div>
          </fieldset>
        ) : null}

        {step === 1 ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Business / brand name" required className="sm:col-span-2">
              {({ id }) => (
                <Input
                  id={id}
                  value={form.brand_name}
                  onChange={(e) => setForm((f) => ({ ...f, brand_name: e.target.value }))}
                  placeholder="Acme Coffee Roasters"
                />
              )}
            </Field>
            <Field label="Legal / company name" description="Defaults to your brand name if left blank.">
              {({ id }) => (
                <Input id={id} value={form.profile.company_name ?? ''} onChange={(e) => setProfile('company_name', e.target.value)} />
              )}
            </Field>
            <Field label="Industry">
              {({ id }) => <Input id={id} value={form.profile.industry ?? ''} onChange={(e) => setProfile('industry', e.target.value || null)} placeholder="Food & beverage" />}
            </Field>
            <Field label="Business type">
              {({ id }) => <Input id={id} value={form.profile.business_type ?? ''} onChange={(e) => setProfile('business_type', e.target.value || null)} placeholder="Retail / B2C" />}
            </Field>
            <Field label="What does your business do?" className="sm:col-span-2">
              {({ id }) => <Textarea id={id} value={form.profile.description ?? ''} onChange={(e) => setProfile('description', e.target.value || null)} placeholder="A short description in plain language." />}
            </Field>
          </div>
        ) : null}

        {step === 2 ? (
          <div className="space-y-4">
            <p className="text-sm text-muted-foreground">
              Add your website or any place customers learn about your business. All optional.
            </p>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Website" className="sm:col-span-2">
                {({ id }) => <Input id={id} value={form.profile.website ?? ''} onChange={(e) => setProfile('website', e.target.value || null)} placeholder="https://example.com" />}
              </Field>
              <Field label="Google Business Profile">
                {({ id }) => <Input id={id} value={form.profile.google_business_profile ?? ''} onChange={(e) => setProfile('google_business_profile', e.target.value || null)} placeholder="Maps listing URL" />}
              </Field>
              <Field label="Other presence" description="Anywhere else people find you.">
                {({ id }) => <Input id={id} value={form.profile.alternative_presence ?? ''} onChange={(e) => setProfile('alternative_presence', e.target.value || null)} placeholder="Local directory, event, etc." />}
              </Field>
              <Field label="Marketplace links" className="sm:col-span-2" description="Amazon, Etsy, app stores, etc.">
                {({ id }) => (
                  <TagInput id={id} value={form.profile.marketplace_links ?? []} onChange={(v) => setProfile('marketplace_links', v)} placeholder="Add a link" />
                )}
              </Field>
            </div>
            <div>
              <p className="mb-2 text-sm font-medium">Social profiles</p>
              <div className="grid gap-3 sm:grid-cols-2">
                {SOCIAL_PLATFORMS.map((platform) => (
                  <Field key={platform} label={platform === 'x' ? 'X (Twitter)' : platform[0]!.toUpperCase() + platform.slice(1)}>
                    {({ id }) => (
                      <Input
                        id={id}
                        value={form.profile.social_links?.[platform] ?? ''}
                        onChange={(e) => setSocial(platform, e.target.value)}
                        placeholder="Profile URL or handle"
                      />
                    )}
                  </Field>
                ))}
              </div>
            </div>
          </div>
        ) : null}

        {step === 3 ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Core problem you solve" className="sm:col-span-2">
              {({ id }) => <Textarea id={id} value={form.profile.core_problem_solved ?? ''} onChange={(e) => setProfile('core_problem_solved', e.target.value || null)} />}
            </Field>
            <Field label="Unique value proposition" className="sm:col-span-2">
              {({ id }) => <Textarea id={id} value={form.profile.unique_value_proposition ?? ''} onChange={(e) => setProfile('unique_value_proposition', e.target.value || null)} />}
            </Field>
            <Field label="Target audience">
              {({ id }) => <Input id={id} value={form.profile.target_audience ?? ''} onChange={(e) => setProfile('target_audience', e.target.value || null)} placeholder="Who you serve" />}
            </Field>
            <Field label="Ideal customer profile">
              {({ id }) => <Input id={id} value={form.profile.ideal_customer_profile ?? ''} onChange={(e) => setProfile('ideal_customer_profile', e.target.value || null)} />}
            </Field>
            <Field label="Markets served" className="sm:col-span-2" description="Cities, regions or countries.">
              {({ id }) => <TagInput id={id} value={form.profile.markets_served ?? []} onChange={(v) => setProfile('markets_served', v)} placeholder="Add a market" />}
            </Field>
            <Field label="Customer pain points" className="sm:col-span-2">
              {({ id }) => <TagInput id={id} value={form.profile.customer_pain_points ?? []} onChange={(v) => setProfile('customer_pain_points', v)} placeholder="Add a pain point" />}
            </Field>
            <Field label="Common objections" className="sm:col-span-2">
              {({ id }) => <TagInput id={id} value={form.profile.common_objections ?? []} onChange={(v) => setProfile('common_objections', v)} placeholder="Add an objection" />}
            </Field>
          </div>
        ) : null}

        {step === 4 ? (
          <div className="space-y-5">
            <div>
              <p className="mb-1 text-sm font-medium">Signal sources</p>
              <p className="mb-3 text-xs text-muted-foreground">
                Which sources scouts should draw from. Fixture connectors are used in this build and are clearly labeled “simulated”.
              </p>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {Object.entries(sourceTypeLabels).map(([value, lbl]) => {
                  const checked = form.preferred_source_types.includes(value);
                  return (
                    <label key={value} className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm">
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(c) =>
                          setForm((f) => {
                            const set = new Set(f.preferred_source_types);
                            if (c) set.add(value);
                            else set.delete(value);
                            return { ...f, preferred_source_types: [...set] };
                          })
                        }
                        aria-label={lbl}
                      />
                      {lbl}
                    </label>
                  );
                })}
              </div>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Campaign goals" className="sm:col-span-2">
                {({ id }) => <TagInput id={id} value={form.profile.campaign_goals ?? []} onChange={(v) => setProfile('campaign_goals', v)} placeholder="Add a goal" />}
              </Field>
              <Field label="Preferred platforms" className="sm:col-span-2">
                {({ id }) => <TagInput id={id} value={form.profile.preferred_platforms ?? []} onChange={(v) => setProfile('preferred_platforms', v)} placeholder="Add a platform" />}
              </Field>
              <Field label="Weekly ad volume" description="Roughly how many ads per week.">
                {({ id }) => (
                  <Input
                    id={id}
                    type="number"
                    min={0}
                    value={form.profile.weekly_ad_volume ?? ''}
                    onChange={(e) => setProfile('weekly_ad_volume', e.target.value === '' ? null : Number(e.target.value))}
                  />
                )}
              </Field>
            </div>
          </div>
        ) : null}

        {step === 5 ? (
          <div className="space-y-4">
            <h2 className="text-lg font-semibold">Review</h2>
            <dl className="divide-y divide-border rounded-md border border-border">
              <ReviewRow label="Business name" value={nameValue || '—'} />
              <ReviewRow label="Presence path" value={PRESENCE_PATHS.find((p) => p.value === form.profile.onboarding_path)?.title ?? 'Not set'} />
              <ReviewRow label="Industry" value={form.profile.industry ?? '—'} />
              <ReviewRow label="Website" value={form.profile.website ?? '—'} />
              <ReviewRow label="Markets served" value={(form.profile.markets_served ?? []).join(', ') || '—'} />
              <ReviewRow
                label="Signal sources"
                value={form.preferred_source_types.map((s) => sourceTypeLabels[s] ?? s).join(', ') || '—'}
              />
            </dl>
            <p className="text-sm text-muted-foreground">
              You can edit any of this later from Campaign Context and Locations.
            </p>
          </div>
        ) : null}
      </div>

      {/* Footer nav */}
      <div className="mt-4 flex items-center justify-between">
        <Button variant="ghost" onClick={prev} disabled={step === 0}>
          <ChevronLeft className="size-4" /> Back
        </Button>
        <span className="text-xs text-muted-foreground">Progress saved automatically</span>
        {step < STEPS.length - 1 ? (
          <Button onClick={next} disabled={!stepValid}>
            Save &amp; continue <ChevronRight className="size-4" />
          </Button>
        ) : (
          <Button onClick={() => submit.mutate()} disabled={!canSubmit || submit.isPending}>
            <Rocket className="size-4" /> Finish onboarding
          </Button>
        )}
      </div>
    </div>
  );
}

function ReviewRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 px-4 py-2.5 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="max-w-[60%] truncate text-right font-medium">{value}</dd>
    </div>
  );
}

export function OnboardingPage() {
  return <RequireWorkspace>{({ workspaceId }) => <WizardInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
