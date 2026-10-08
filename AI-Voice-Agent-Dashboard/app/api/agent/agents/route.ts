import { NextResponse } from 'next/server';
import { DograhClient, dograh } from '@/lib/dograh';
import { VERTICALS, VERTICAL_LABELS, type Vertical } from '@/lib/verticals';

/**
 * Which business lines actually have a calling agent behind them.
 *
 * Without this the AI Agent page could only show one agent's script and gave no
 * way to tell whether Solar had an agent at all - the first sign would have been
 * a campaign refusing to launch. Each row here says: configured or not, which
 * Dograh workflow, and whether that workflow really answers.
 *
 * `reachable` is deliberately separate from `configured`: an id can be set in
 * the environment and still point at a workflow that was deleted or renamed, and
 * those two failures need different fixes.
 */
export const dynamic = 'force-dynamic';

function workflowIdFor(vertical: Vertical): number | null {
  const raw =
    vertical === 'loan'
      ? process.env.DOGRAH_WORKFLOW_ID_LOAN ?? process.env.DOGRAH_WORKFLOW_ID
      : process.env[`DOGRAH_WORKFLOW_ID_${vertical.toUpperCase()}`];

  const id = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(id) ? id : null;
}

export async function GET() {
  try {
    const configured = DograhClient.isConfigured();

    const agents = await Promise.all(
      VERTICALS.map(async (vertical) => {
        const workflowId = workflowIdFor(vertical);
        const base = {
          vertical,
          label: VERTICAL_LABELS[vertical],
          workflow_id: workflowId,
          configured: workflowId != null,
          reachable: false,
          name: null as string | null,
          detail: workflowId == null ? 'No agent built yet' : 'Not checked',
        };

        if (!configured || workflowId == null) {
          if (!configured) base.detail = 'Calling provider is not configured';
          return base;
        }

        try {
          const workflow = await dograh.getWorkflow(workflowId);
          return {
            ...base,
            reachable: true,
            name: workflow?.name ?? null,
            detail: 'Ready',
          };
        } catch {
          // A configured-but-unreachable agent is worse than an unbuilt one:
          // campaigns will launch and then fail at the provider, so it is
          // called out rather than folded in with "not built".
          return { ...base, detail: `Workflow ${workflowId} did not respond` };
        }
      }),
    );

    return NextResponse.json({ provider_configured: configured, agents });
  } catch (error) {
    console.error('[agent/agents] unexpected', error);
    return NextResponse.json({ error: 'Internal Server Error' }, { status: 500 });
  }
}
