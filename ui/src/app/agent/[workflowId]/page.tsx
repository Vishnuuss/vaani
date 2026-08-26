'use client';

/**
 * Single-prompt agent editor — the "Vapi / OmniDimension" way of building an
 * agent: one prompt plus real tools, with no graph, no nodes and no
 * connections anywhere in the interface.
 *
 * It saves into Dograh's normal workflow storage (one startCall node, zero
 * edges), so campaigns, telephony, Redis, websockets and reporting all keep
 * working untouched. The canvas editor still exists at /workflow/[id] for
 * anyone who wants it; this page never opens it.
 */

import { ArrowLeft, Loader2, Plus, Save, Trash2, Wrench } from 'lucide-react';
import { useParams, useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';

import {
    getWorkflowApiV1WorkflowFetchWorkflowIdGet,
    listToolsApiV1ToolsGet,
    updateWorkflowApiV1WorkflowWorkflowIdPut,
} from '@/client/sdk.gen';
import type { ToolResponse } from '@/client/types.gen';
import SpinLoader from '@/components/SpinLoader';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { detailFromError } from '@/lib/apiError';
import { useAuth } from '@/lib/auth';
import logger from '@/lib/logger';

import {
    definitionToDraft,
    draftToDefinition,
    isSinglePromptCompatible,
    validateDraft,
} from './mapping';
import {
    type AgentDraft,
    EMPTY_DRAFT,
    type ExtractionVariableType,
    type FlowDefinition,
} from './types';

export default function AgentEditorPage() {
    const params = useParams();
    const router = useRouter();
    const workflowId = Number(params.workflowId);
    const { user, redirectToLogin, loading: authLoading } = useAuth();

    const [draft, setDraft] = useState<AgentDraft>(EMPTY_DRAFT);
    const [definition, setDefinition] = useState<FlowDefinition | null>(null);
    const [tools, setTools] = useState<ToolResponse[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [blocked, setBlocked] = useState(false);

    useEffect(() => {
        if (!authLoading && !user) redirectToLogin();
    }, [authLoading, user, redirectToLogin]);

    useEffect(() => {
        const load = async () => {
            if (!user || Number.isNaN(workflowId)) return;
            try {
                const [workflowRes, toolsRes] = await Promise.all([
                    getWorkflowApiV1WorkflowFetchWorkflowIdGet({
                        path: { workflow_id: workflowId },
                    }),
                    listToolsApiV1ToolsGet(),
                ]);

                if (workflowRes.error) {
                    setError(detailFromError(workflowRes.error, 'Failed to load the agent'));
                    return;
                }
                const workflow = workflowRes.data;
                if (!workflow) {
                    setError('Agent not found');
                    return;
                }

                const def = (workflow.workflow_definition ?? null) as FlowDefinition | null;
                if (!isSinglePromptCompatible(def)) {
                    setBlocked(true);
                    return;
                }

                setDefinition(def);
                setDraft(definitionToDraft(def, workflow.name ?? ''));
                if (!toolsRes.error && toolsRes.data) {
                    setTools(toolsRes.data as ToolResponse[]);
                }
            } catch (err) {
                logger.error(`Error loading agent: ${err}`);
                setError('Failed to load the agent');
            } finally {
                setLoading(false);
            }
        };
        void load();
    }, [user, workflowId]);

    const update = useCallback(<K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) => {
        setDraft((prev) => ({ ...prev, [key]: value }));
    }, []);

    const handleSave = useCallback(async () => {
        const problem = validateDraft(draft);
        if (problem) {
            toast.error(problem);
            return;
        }
        setSaving(true);
        try {
            const response = await updateWorkflowApiV1WorkflowWorkflowIdPut({
                path: { workflow_id: workflowId },
                body: {
                    name: draft.name.trim(),
                    workflow_definition: draftToDefinition(draft, definition) as unknown as {
                        [key: string]: unknown;
                    },
                },
            });
            if (response.error) {
                toast.error(detailFromError(response.error, 'Could not save the agent'));
                return;
            }
            const saved = response.data?.workflow_definition as FlowDefinition | undefined;
            if (saved) setDefinition(saved);
            toast.success('Agent saved');
        } catch (err) {
            logger.error(`Error saving agent: ${err}`);
            toast.error('Could not save the agent');
        } finally {
            setSaving(false);
        }
    }, [draft, definition, workflowId]);

    const endCallTools = useMemo(
        () => tools.filter((t) => t.category === 'end_call'),
        [tools],
    );
    const hasEndCallSelected = useMemo(
        () => endCallTools.some((t) => draft.toolUuids.includes(t.tool_uuid)),
        [endCallTools, draft.toolUuids],
    );

    if (authLoading || loading) return <SpinLoader />;

    if (blocked) {
        return (
            <div className="container mx-auto max-w-3xl px-4 py-10">
                <Card className="border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40">
                    <CardHeader>
                        <CardTitle>This agent was built on the canvas</CardTitle>
                        <CardDescription className="text-amber-900 dark:text-amber-200">
                            It has more than one step or has connections between steps. Opening it
                            here would flatten it into a single prompt and lose that structure, so
                            this editor will not touch it.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="flex flex-wrap gap-3">
                        <Button onClick={() => router.push(`/workflow/${workflowId}`)}>
                            Open in the canvas editor
                        </Button>
                        <Button variant="outline" onClick={() => router.push('/workflow')}>
                            Back to agents
                        </Button>
                    </CardContent>
                </Card>
            </div>
        );
    }

    if (error) {
        return (
            <div className="container mx-auto max-w-3xl px-4 py-10">
                <Card>
                    <CardHeader>
                        <CardTitle>Something went wrong</CardTitle>
                        <CardDescription>{error}</CardDescription>
                    </CardHeader>
                </Card>
            </div>
        );
    }

    return (
        <div className="container mx-auto max-w-3xl px-4 py-8">
            <div className="mb-8 flex flex-wrap items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                    <Button
                        variant="ghost"
                        size="icon"
                        aria-label="Back to agents"
                        onClick={() => router.push('/workflow')}
                    >
                        <ArrowLeft className="h-4 w-4" />
                    </Button>
                    <div>
                        <h1 className="text-2xl font-semibold tracking-tight">Agent</h1>
                        <p className="text-sm text-muted-foreground">
                            One prompt. No steps, no connections.
                        </p>
                    </div>
                </div>
                <Button onClick={handleSave} disabled={saving}>
                    {saving ? (
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    ) : (
                        <Save className="mr-2 h-4 w-4" />
                    )}
                    Save
                </Button>
            </div>

            <div className="space-y-6">
                <Card>
                    <CardHeader>
                        <CardTitle className="text-lg">Basics</CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-5">
                        <div className="space-y-2">
                            <Label htmlFor="agent-name">Name</Label>
                            <Input
                                id="agent-name"
                                value={draft.name}
                                onChange={(e) => update('name', e.target.value)}
                                placeholder="BS Wealth — loan calls"
                            />
                        </div>
                        <div className="space-y-2">
                            <Label htmlFor="agent-greeting">First message</Label>
                            <Textarea
                                id="agent-greeting"
                                value={draft.greeting}
                                onChange={(e) => update('greeting', e.target.value)}
                                placeholder="Namaskaram, BS Wealth Finance nunchi Priya matladutunnanu."
                                rows={2}
                            />
                            <p className="text-xs text-muted-foreground">
                                Spoken the moment the call connects, before the model thinks.
                            </p>
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle className="text-lg">Your business</CardTitle>
                        <CardDescription>
                            What this agent sells, what it may claim, and what it must never say.
                            Vaani wraps this automatically with three more layers you never write:
                            <span className="font-medium"> persona &amp; voice</span>,
                            <span className="font-medium"> sales psychology &amp; objection
                            handling</span>, and
                            <span className="font-medium"> the call mission</span>.
                            The whole thing is compiled once and never changes mid-call.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2">
                        <Textarea
                            id="agent-prompt"
                            aria-label="Agent prompt"
                            value={draft.prompt}
                            onChange={(e) => update('prompt', e.target.value)}
                            placeholder="You are Priya, calling on behalf of BS Wealth Finance..."
                            className="min-h-[420px] font-mono text-sm leading-relaxed"
                        />
                        <div className="flex flex-wrap items-center justify-between gap-2">
                            <p className="text-xs text-muted-foreground">
                                {draft.prompt.length.toLocaleString()} characters — this is
                                Layer 3 of 4
                            </p>
                            <p className="text-xs text-muted-foreground">
                                Layers 1, 2 and 4 are identical for every agent, so they stay in
                                the model&apos;s cache instead of being re-billed each turn.
                            </p>
                        </div>
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle className="text-lg">Tools</CardTitle>
                        <CardDescription>
                            What the agent can actually do — hang up, transfer to a human, call your
                            APIs.
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        {tools.length === 0 ? (
                            <div className="rounded-md border border-dashed p-6 text-center">
                                <Wrench className="mx-auto mb-2 h-5 w-5 text-muted-foreground" />
                                <p className="text-sm text-muted-foreground">
                                    No tools yet. Create an{' '}
                                    <span className="font-medium">End Call</span> tool so the agent
                                    can hang up on its own.
                                </p>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    className="mt-3"
                                    onClick={() => router.push('/tools')}
                                >
                                    Manage tools
                                </Button>
                            </div>
                        ) : (
                            <div className="space-y-3">
                                {tools.map((tool) => (
                                    <label
                                        key={tool.tool_uuid}
                                        htmlFor={`tool-${tool.tool_uuid}`}
                                        className="flex cursor-pointer items-start gap-3 rounded-md border p-3 transition-colors hover:bg-accent/50"
                                    >
                                        <Checkbox
                                            id={`tool-${tool.tool_uuid}`}
                                            checked={draft.toolUuids.includes(tool.tool_uuid)}
                                            onCheckedChange={(checked) => {
                                                update(
                                                    'toolUuids',
                                                    checked
                                                        ? [...draft.toolUuids, tool.tool_uuid]
                                                        : draft.toolUuids.filter(
                                                              (id) => id !== tool.tool_uuid,
                                                          ),
                                                );
                                            }}
                                            className="mt-0.5"
                                        />
                                        <div className="min-w-0 flex-1">
                                            <div className="flex flex-wrap items-center gap-2">
                                                <span className="text-sm font-medium">
                                                    {tool.name}
                                                </span>
                                                <Badge variant="secondary" className="text-xs">
                                                    {tool.category}
                                                </Badge>
                                            </div>
                                            {tool.description ? (
                                                <p className="mt-1 text-xs text-muted-foreground">
                                                    {tool.description}
                                                </p>
                                            ) : null}
                                        </div>
                                    </label>
                                ))}
                            </div>
                        )}

                        {tools.length > 0 && !hasEndCallSelected ? (
                            <p className="rounded-md bg-amber-50 p-3 text-xs text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
                                No <span className="font-medium">End Call</span> tool is attached.
                                Without one the agent cannot hang up by itself — the call ends only
                                when the customer hangs up or the time limit is reached.
                            </p>
                        ) : null}
                    </CardContent>
                </Card>

                <Card>
                    <CardHeader>
                        <div className="flex items-start justify-between gap-4">
                            <div>
                                <CardTitle className="text-lg">Information to collect</CardTitle>
                                <CardDescription>
                                    Pulled out of the conversation when the call ends.
                                </CardDescription>
                            </div>
                            <Switch
                                aria-label="Collect information"
                                checked={draft.extractionEnabled}
                                onCheckedChange={(checked) => update('extractionEnabled', checked)}
                            />
                        </div>
                    </CardHeader>
                    {draft.extractionEnabled ? (
                        <CardContent className="space-y-5">
                            <div className="space-y-2">
                                <Label htmlFor="extraction-prompt">Instructions (optional)</Label>
                                <Textarea
                                    id="extraction-prompt"
                                    value={draft.extractionPrompt}
                                    onChange={(e) => update('extractionPrompt', e.target.value)}
                                    placeholder="Read the conversation and pull out the customer's income and city."
                                    rows={2}
                                />
                            </div>

                            <div className="space-y-3">
                                {draft.extractionVariables.map((variable, index) => (
                                    <div
                                        key={index}
                                        className="grid grid-cols-1 gap-2 rounded-md border p-3 sm:grid-cols-[minmax(0,1fr)_130px_minmax(0,1.5fr)_auto] sm:items-center"
                                    >
                                        <Input
                                            aria-label="Field name"
                                            value={variable.name}
                                            placeholder="monthly_income"
                                            onChange={(e) => {
                                                const next = [...draft.extractionVariables];
                                                next[index] = { ...variable, name: e.target.value };
                                                update('extractionVariables', next);
                                            }}
                                        />
                                        <Select
                                            value={variable.type}
                                            onValueChange={(value) => {
                                                const next = [...draft.extractionVariables];
                                                next[index] = {
                                                    ...variable,
                                                    type: value as ExtractionVariableType,
                                                };
                                                update('extractionVariables', next);
                                            }}
                                        >
                                            <SelectTrigger aria-label="Field type">
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="string">Text</SelectItem>
                                                <SelectItem value="number">Number</SelectItem>
                                                <SelectItem value="boolean">Yes / No</SelectItem>
                                            </SelectContent>
                                        </Select>
                                        <Input
                                            aria-label="What to look for"
                                            value={variable.prompt}
                                            placeholder="Monthly income in rupees"
                                            onChange={(e) => {
                                                const next = [...draft.extractionVariables];
                                                next[index] = {
                                                    ...variable,
                                                    prompt: e.target.value,
                                                };
                                                update('extractionVariables', next);
                                            }}
                                        />
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            aria-label={`Remove ${variable.name || 'field'}`}
                                            onClick={() =>
                                                update(
                                                    'extractionVariables',
                                                    draft.extractionVariables.filter(
                                                        (_, i) => i !== index,
                                                    ),
                                                )
                                            }
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </Button>
                                    </div>
                                ))}
                            </div>

                            <Button
                                variant="outline"
                                size="sm"
                                onClick={() =>
                                    update('extractionVariables', [
                                        ...draft.extractionVariables,
                                        { name: '', type: 'string', prompt: '' },
                                    ])
                                }
                            >
                                <Plus className="mr-2 h-4 w-4" />
                                Add field
                            </Button>
                        </CardContent>
                    ) : null}
                </Card>

                <Card>
                    <CardHeader>
                        <CardTitle className="text-lg">Call behaviour</CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <div className="flex items-center justify-between gap-4">
                            <div>
                                <Label htmlFor="allow-interrupt">Let the customer interrupt</Label>
                                <p className="text-xs text-muted-foreground">
                                    The agent stops talking the moment the customer speaks.
                                </p>
                            </div>
                            <Switch
                                id="allow-interrupt"
                                checked={draft.allowInterrupt}
                                onCheckedChange={(checked) => update('allowInterrupt', checked)}
                            />
                        </div>
                        <div className="flex items-center justify-between gap-4">
                            <div>
                                <Label htmlFor="detect-voicemail">Detect voicemail</Label>
                                <p className="text-xs text-muted-foreground">
                                    Hang up instead of talking to an answering machine.
                                </p>
                            </div>
                            <Switch
                                id="detect-voicemail"
                                checked={draft.detectVoicemail}
                                onCheckedChange={(checked) => update('detectVoicemail', checked)}
                            />
                        </div>
                    </CardContent>
                </Card>
            </div>
        </div>
    );
}
