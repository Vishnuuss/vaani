/**
 * Types for the single-prompt ("Vapi style") agent editor.
 *
 * The editor never exposes the graph. It reads and writes exactly ONE
 * `startCall` node with ZERO edges, which is the storage shape Dograh's engine
 * already treats as a single-prompt agent: with no outgoing edges the engine
 * never calls `set_node()` again, so the system prompt is composed once and is
 * never swapped mid-call, and no edge/transition tools are registered.
 */

export type ExtractionVariableType = "string" | "number" | "boolean";

export interface ExtractionVariable {
    name: string;
    type: ExtractionVariableType;
    prompt: string;
}

/** The subset of startCall node data this editor owns. */
export interface AgentDraft {
    name: string;
    greeting: string;
    prompt: string;
    allowInterrupt: boolean;
    detectVoicemail: boolean;
    toolUuids: string[];
    extractionEnabled: boolean;
    extractionPrompt: string;
    extractionVariables: ExtractionVariable[];
}

export interface FlowNodeLike {
    id: string;
    type: string;
    position: { x: number; y: number };
    data: Record<string, unknown>;
}

export interface FlowDefinition {
    nodes: FlowNodeLike[];
    edges: unknown[];
    viewport?: { x: number; y: number; zoom: number };
    [key: string]: unknown;
}

export const SINGLE_NODE_ID = "agent";

export const EMPTY_DRAFT: AgentDraft = {
    name: "",
    greeting: "",
    prompt: "",
    allowInterrupt: true,
    detectVoicemail: false,
    toolUuids: [],
    extractionEnabled: false,
    extractionPrompt: "",
    extractionVariables: [],
};
