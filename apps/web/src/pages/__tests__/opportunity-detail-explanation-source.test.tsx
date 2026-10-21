import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { DEGRADED_WHY, degradedOpportunityId, opportunityDetailFixture } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// P6-LLM-5: the detail page must label explanation copy that did not come from the
// model, so placeholder text is never read as evidence-backed AI reasoning.
const DEGRADED_BADGE = 'AI explanation unavailable';
const DEGRADED_NOTE = /AI explanation unavailable for this run\./i;
const RULE_BASED_NOTE = /no AI model\s+was consulted/i;

describe('opportunity detail explanation source', () => {
  it('labels a degraded explanation as placeholder copy, not AI reasoning', async () => {
    const screen = renderApp(<App />, { route: `/opportunities/${degradedOpportunityId}` });
    expect(
      await screen.findByRole('heading', { name: /Dallas customers want faster delivery 2/i }),
    ).toBeInTheDocument();

    expect(screen.getByText(DEGRADED_BADGE)).toBeInTheDocument();
    const note = screen.getByRole('note');
    expect(note).toHaveTextContent(DEGRADED_NOTE);
    expect(note).toHaveTextContent(/placeholder copy, not AI reasoning/i);
    // The stored placeholder is shown as-is and no inference is fabricated.
    expect(screen.getByText(DEGRADED_WHY)).toBeInTheDocument();
    expect(screen.getByText(/No inference generated\./i)).toBeInTheDocument();
    expect(screen.queryByText(/suggested creative angles/i)).not.toBeInTheDocument();
  });

  it('shows no degradation label on model-generated output', async () => {
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    expect(await screen.findByText(/unmet, localized need/i)).toBeInTheDocument();

    expect(screen.queryByText(DEGRADED_BADGE)).not.toBeInTheDocument();
    expect(screen.queryByText(DEGRADED_NOTE)).not.toBeInTheDocument();
    expect(screen.queryByText(RULE_BASED_NOTE)).not.toBeInTheDocument();
  });

  it.each([
    ['withheld', 'AI explanation withheld', /did not pass safety screening/i],
    ['budget_limited', 'AI usage limit reached', /reached its AI usage limit/i],
  ] as const)('labels %s explanation copy as placeholder', async (source, badge, reason) => {
    server.use(
      http.get(`*${API_PREFIX}/workspaces/:ws/opportunities/opp-loc-dallas-0`, () =>
        HttpResponse.json({
          ...opportunityDetailFixture('opp-loc-dallas-0'),
          explanation_source: source,
          ai_inference: null,
          suggested_angles: [],
        }),
      ),
    );
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    expect(await screen.findByText(badge)).toBeInTheDocument();
    const note = screen.getByRole('note');
    expect(note).toHaveTextContent(reason);
    expect(note).toHaveTextContent(/placeholder copy, not AI reasoning/i);
    expect(screen.queryByText(DEGRADED_BADGE)).not.toBeInTheDocument();
  });

  it('says when no AI model was consulted', async () => {
    server.use(
      http.get(`*${API_PREFIX}/workspaces/:ws/opportunities/opp-loc-dallas-0`, () =>
        HttpResponse.json({
          ...opportunityDetailFixture('opp-loc-dallas-0'),
          explanation_source: 'rule_based',
        }),
      ),
    );
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    expect(await screen.findByText(RULE_BASED_NOTE)).toBeInTheDocument();
    expect(screen.queryByText(DEGRADED_BADGE)).not.toBeInTheDocument();
  });

  it('adds no label when the source was not recorded', async () => {
    server.use(
      http.get(`*${API_PREFIX}/workspaces/:ws/opportunities/opp-loc-dallas-0`, () =>
        HttpResponse.json({
          ...opportunityDetailFixture('opp-loc-dallas-0'),
          explanation_source: null,
        }),
      ),
    );
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    expect(await screen.findByText(/unmet, localized need/i)).toBeInTheDocument();
    expect(screen.queryByText(DEGRADED_BADGE)).not.toBeInTheDocument();
    expect(screen.queryByText(RULE_BASED_NOTE)).not.toBeInTheDocument();
  });
});
