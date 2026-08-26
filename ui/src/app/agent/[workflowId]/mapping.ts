/**
 * Pure mapping between a Dograh workflow definition and the single-prompt
 * editor's draft. Kept free of React so it can be unit tested directly.
 */

import {
    type AgentDraft,
    EMPTY_DRAFT,
    type ExtractionVariable,
    type ExtractionVariableType,
    type FlowDefinition,
    type FlowNodeLike,
    SINGLE_NODE_ID,
} from "./types";

const START_NODE_TYPE = "startCall";
const VALID_TYPES: ExtractionVariableType[] = ["string", "number", "boolean"];

function asString(value: unknown, fallback = ""): string {
    return typeof value === "string" ? value : fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
    return typeof value === "boolean" ? value : fallback;
}

function asStringArray(value: unknown): string[] {
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function asExtractionVariables(value: unknown): ExtractionVariable[] {
    if (!Array.isArray(value)) return [];
    return value.flatMap((raw): ExtractionVariable[] => {
        if (typeof raw !== "object" || raw === null) return [];
        const item = raw as Record<string, unknown>;
        const name = asString(item.name).trim();
        if (!name) return [];
        const rawType = asString(item.type, "string");
        const type = (VALID_TYPES as string[]).includes(rawType)
            ? (rawType as ExtractionVariableType)
            : "string";
        return [{ name, type, prompt: asString(item.prompt) }];
    });
}

export function getStartNode(definition: FlowDefinition | null): FlowNodeLike | null {
    if (!definition?.nodes) return null;
    return definition.nodes.find((node) => node.type === START_NODE_TYPE) ?? null;
}

/**
 * A definition is editable here only when it is already the single-prompt
 * shape: one startCall node and no edges. Anything with extra nodes or any
 * connection is a graph agent built on the canvas — this editor refuses it
 * rather than flattening someone's work.
 */
export function isSinglePromptCompatible(definition: FlowDefinition | null): boolean {
    if (!definition) return true; // brand new / empty workflow
    const nodes = definition.nodes ?? [];
    const edges = definition.edges ?? [];
    if (edges.length > 0) return false;
    const meaningful = nodes.filter((node) => node.type !== "globalNode");
    return meaningful.length <= 1 && meaningful.every((n) => n.type === START_NODE_TYPE);
}

export function definitionToDraft(
    definition: FlowDefinition | null,
    workflowName: string,
): AgentDraft {
    const node = getStartNode(definition);
    const data = (node?.data ?? {}) as Record<string, unknown>;
    return {
        ...EMPTY_DRAFT,
        name: workflowName,
        greeting: asString(data.greeting),
        prompt: asString(data.prompt),
        allowInterrupt: asBoolean(data.allow_interrupt, true),
        detectVoicemail: asBoolean(data.detect_voicemail, false),
        toolUuids: asStringArray(data.tool_uuids),
        extractionEnabled: asBoolean(data.extraction_enabled, false),
        extractionPrompt: asString(data.extraction_prompt),
        extractionVariables: asExtractionVariables(data.extraction_variables),
    };
}

/**
 * Build the workflow definition for a draft: exactly one startCall node, no
 * edges. Any pre-existing node data we do not own is preserved so a future
 * Dograh field added to startCall survives a save from this editor.
 */
export function draftToDefinition(
    draft: AgentDraft,
    previous: FlowDefinition | null,
): FlowDefinition {
    const existing = getStartNode(previous);
    const preserved = { ...(existing?.data ?? {}) };

    const cleanedVariables = draft.extractionVariables
        .map((v) => ({ ...v, name: v.name.trim() }))
        .filter((v) => v.name.length > 0);

    const data: Record<string, unknown> = {
        ...preserved,
        name: "Agent",
        prompt: draft.prompt,
        greeting: draft.greeting,
        greeting_type: "text",
        allow_interrupt: draft.allowInterrupt,
        detect_voicemail: draft.detectVoicemail,
        add_global_prompt: false,
        is_start: true,
        tool_uuids: draft.toolUuids,
        extraction_enabled: draft.extractionEnabled && cleanedVariables.length > 0,
        extraction_prompt: draft.extractionPrompt,
        extraction_variables: cleanedVariables,
    };

    return {
        nodes: [
            {
                id: existing?.id ?? SINGLE_NODE_ID,
                type: START_NODE_TYPE,
                position: existing?.position ?? { x: 0, y: 0 },
                data,
            },
        ],
        edges: [],
        viewport: previous?.viewport ?? { x: 0, y: 0, zoom: 1 },
    };
}

export function validateDraft(draft: AgentDraft): string | null {
    if (!draft.name.trim()) return "Give the agent a name.";
    if (!draft.prompt.trim()) return "The prompt cannot be empty.";
    if (draft.extractionEnabled) {
        const named = draft.extractionVariables.filter((v) => v.name.trim());
        if (named.length === 0) {
            return "Add at least one field to collect, or turn off information collection.";
        }
        const seen = new Set<string>();
        for (const v of named) {
            const key = v.name.trim().toLowerCase();
            if (seen.has(key)) return `Duplicate field name: ${v.name.trim()}`;
            seen.add(key);
        }
    }
    return null;
}
