import { describe, expect, it } from 'vitest';

import {
    definitionToDraft,
    draftToDefinition,
    getStartNode,
    isSinglePromptCompatible,
    validateDraft,
} from './mapping';
import { EMPTY_DRAFT, type FlowDefinition } from './types';

const singleNodeDef: FlowDefinition = {
    nodes: [
        {
            id: 'agent',
            type: 'startCall',
            position: { x: 0, y: 0 },
            data: {
                prompt: 'You are Priya.',
                greeting: 'Namaskaram.',
                allow_interrupt: true,
                detect_voicemail: false,
                tool_uuids: ['tool-1'],
                extraction_enabled: true,
                extraction_prompt: 'Pull out income.',
                extraction_variables: [
                    { name: 'monthly_income', type: 'number', prompt: 'Income in INR' },
                ],
                some_future_dograh_field: 'keep me',
            },
        },
    ],
    edges: [],
};

const graphDef: FlowDefinition = {
    nodes: [
        { id: '1', type: 'startCall', position: { x: 0, y: 0 }, data: {} },
        { id: '2', type: 'agentNode', position: { x: 0, y: 0 }, data: {} },
        { id: '3', type: 'endCall', position: { x: 0, y: 0 }, data: {} },
    ],
    edges: [{ id: 'e1', source: '1', target: '2' }],
};

describe('isSinglePromptCompatible', () => {
    it('accepts an empty/new workflow', () => {
        expect(isSinglePromptCompatible(null)).toBe(true);
    });

    it('accepts one startCall node with no edges', () => {
        expect(isSinglePromptCompatible(singleNodeDef)).toBe(true);
    });

    it('ignores a globalNode, which carries no edges', () => {
        expect(
            isSinglePromptCompatible({
                nodes: [
                    ...singleNodeDef.nodes,
                    { id: 'g', type: 'globalNode', position: { x: 0, y: 0 }, data: {} },
                ],
                edges: [],
            }),
        ).toBe(true);
    });

    it('refuses a real graph agent so it cannot be flattened', () => {
        expect(isSinglePromptCompatible(graphDef)).toBe(false);
    });

    it('refuses any definition that has edges at all', () => {
        expect(
            isSinglePromptCompatible({
                nodes: singleNodeDef.nodes,
                edges: [{ id: 'e', source: 'agent', target: 'agent' }],
            }),
        ).toBe(false);
    });
});

describe('definitionToDraft', () => {
    it('reads the node data into the editor draft', () => {
        const draft = definitionToDraft(singleNodeDef, 'BS Wealth');
        expect(draft.name).toBe('BS Wealth');
        expect(draft.prompt).toBe('You are Priya.');
        expect(draft.greeting).toBe('Namaskaram.');
        expect(draft.toolUuids).toEqual(['tool-1']);
        expect(draft.extractionEnabled).toBe(true);
        expect(draft.extractionVariables).toEqual([
            { name: 'monthly_income', type: 'number', prompt: 'Income in INR' },
        ]);
    });

    it('falls back cleanly on a brand new workflow', () => {
        const draft = definitionToDraft(null, 'New agent');
        expect(draft.prompt).toBe('');
        expect(draft.allowInterrupt).toBe(true);
        expect(draft.extractionVariables).toEqual([]);
    });

    it('drops malformed extraction variables instead of throwing', () => {
        const draft = definitionToDraft(
            {
                nodes: [
                    {
                        id: 'agent',
                        type: 'startCall',
                        position: { x: 0, y: 0 },
                        data: {
                            extraction_variables: [
                                null,
                                { name: '' },
                                { name: 'city', type: 'nonsense' },
                            ],
                        },
                    },
                ],
                edges: [],
            },
            'x',
        );
        expect(draft.extractionVariables).toEqual([
            { name: 'city', type: 'string', prompt: '' },
        ]);
    });
});

describe('draftToDefinition', () => {
    it('always writes exactly one node and zero edges', () => {
        const draft = { ...EMPTY_DRAFT, name: 'A', prompt: 'hello' };
        const def = draftToDefinition(draft, null);
        expect(def.nodes).toHaveLength(1);
        expect(def.nodes[0].type).toBe('startCall');
        expect(def.edges).toEqual([]);
    });

    it('round-trips a draft without losing values', () => {
        const draft = definitionToDraft(singleNodeDef, 'BS Wealth');
        const roundTripped = definitionToDraft(
            draftToDefinition(draft, singleNodeDef),
            'BS Wealth',
        );
        expect(roundTripped).toEqual(draft);
    });

    it('preserves unknown node fields it does not own', () => {
        const draft = definitionToDraft(singleNodeDef, 'BS Wealth');
        const def = draftToDefinition(draft, singleNodeDef);
        expect(def.nodes[0].data.some_future_dograh_field).toBe('keep me');
    });

    it('keeps the existing node id and position', () => {
        const draft = definitionToDraft(singleNodeDef, 'BS Wealth');
        const def = draftToDefinition(draft, singleNodeDef);
        expect(def.nodes[0].id).toBe('agent');
    });

    it('drops blank field names and disables extraction when none remain', () => {
        const draft = {
            ...EMPTY_DRAFT,
            prompt: 'p',
            extractionEnabled: true,
            extractionVariables: [{ name: '   ', type: 'string' as const, prompt: '' }],
        };
        const def = draftToDefinition(draft, null);
        expect(def.nodes[0].data.extraction_variables).toEqual([]);
        expect(def.nodes[0].data.extraction_enabled).toBe(false);
    });

    it('marks the node as the start of the call', () => {
        const def = draftToDefinition({ ...EMPTY_DRAFT, prompt: 'p' }, null);
        expect(def.nodes[0].data.is_start).toBe(true);
    });
});

describe('getStartNode', () => {
    it('finds the startCall node', () => {
        expect(getStartNode(singleNodeDef)?.id).toBe('agent');
    });

    it('returns null when there is none', () => {
        expect(getStartNode({ nodes: [], edges: [] })).toBeNull();
    });
});

describe('validateDraft', () => {
    it('requires a name', () => {
        expect(validateDraft({ ...EMPTY_DRAFT, prompt: 'p' })).toMatch(/name/i);
    });

    it('requires a prompt', () => {
        expect(validateDraft({ ...EMPTY_DRAFT, name: 'A' })).toMatch(/prompt/i);
    });

    it('rejects duplicate field names', () => {
        const problem = validateDraft({
            ...EMPTY_DRAFT,
            name: 'A',
            prompt: 'p',
            extractionEnabled: true,
            extractionVariables: [
                { name: 'city', type: 'string', prompt: '' },
                { name: 'City', type: 'string', prompt: '' },
            ],
        });
        expect(problem).toMatch(/duplicate/i);
    });

    it('passes a complete draft', () => {
        expect(
            validateDraft({
                ...EMPTY_DRAFT,
                name: 'A',
                prompt: 'p',
                extractionEnabled: true,
                extractionVariables: [{ name: 'city', type: 'string', prompt: '' }],
            }),
        ).toBeNull();
    });
});
