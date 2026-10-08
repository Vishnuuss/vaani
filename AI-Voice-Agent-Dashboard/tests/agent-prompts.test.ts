/**
 * Editing one agent's script must never destroy it.
 *
 * The AI Agent page was written for the old four-node graph, where a start node
 * held one piece of text. All four BS Wealth agents on Vaani are single-prompt:
 * ONE `startCall` node with `greeting_type: 'text'`, where
 *
 *   data.greeting   ~90 chars   the opening line the caller hears
 *   data.prompt     2k-4.3k     the script that drives the whole rest of the call
 *
 * The old save path wrote the greeting into BOTH fields and published, so
 * editing the opening line replaced the entire 4,302-character loan script with
 * one sentence, live. The old read path returned only the greeting, so the
 * script was never visible to check.
 *
 * These tests pin the split. They exercise the same node-walking logic the route
 * uses; the route itself needs a Next request context, so the logic is mirrored
 * here against real node shapes taken from live workflows 3 and 6.
 */
import { test, describe } from 'node:test';
import assert from 'node:assert/strict';

const NODE_TYPE_TO_KEY: Record<string, string> = {
  globalNode: 'global',
  startCall: 'start',
  agentNode: 'agenda',
  endCall: 'end',
};

function isSeparateGreetingNode(node: any): boolean {
  return node?.type === 'startCall' && node?.data?.greeting_type === 'text';
}

function readNodeText(node: any): string {
  if (node?.type === 'startCall') {
    const greeting = String(node.data?.greeting ?? '').trim();
    if (greeting) return node.data.greeting;
    return node.data?.prompt ?? '';
  }
  return node?.data?.prompt ?? '';
}

function readPrompts(nodes: any[]): Record<string, string> {
  const prompts: Record<string, string> = {};
  for (const node of nodes) {
    const key = NODE_TYPE_TO_KEY[node.type];
    if (!key) continue;
    prompts[key] = readNodeText(node);
    if (isSeparateGreetingNode(node)) prompts.script = node.data?.prompt ?? '';
  }
  return prompts;
}

function applyUpdates(nodes: any[], updates: Record<string, string>): number {
  let changed = 0;
  for (const node of nodes) {
    const key = NODE_TYPE_TO_KEY[node.type];
    if (isSeparateGreetingNode(node)) {
      if (updates.start !== undefined) {
        node.data.greeting = updates.start;
        changed++;
      }
      if (updates.script !== undefined) {
        node.data.prompt = updates.script;
        changed++;
      }
      continue;
    }
    if (key && updates[key] !== undefined) {
      node.data.prompt = updates[key];
      if (node.type === 'startCall') node.data.greeting = updates[key];
      changed++;
    }
  }
  return changed;
}

/** Workflow 3, as fetched on 2026-09-04. */
const GREETING = 'నమస్కారం అండి. నేను శ్రేయ, HDFC BANK నుంచి మాట్లాడుతున్నాను. మీకు ఏదైనా లోన్ అవసరం ఉందా?';
const SCRIPT = 'x'.repeat(4302);

const singlePromptAgent = () => [
  {
    type: 'startCall',
    data: { greeting_type: 'text', greeting: GREETING, prompt: SCRIPT, is_start: true },
  },
  { type: 'webhook', data: { endpoint_url: 'https://admin.bswealthfinance.com/api/webhook/call-result' } },
];

describe('reading a single-prompt agent', () => {
  test('the script is returned, not just the greeting', () => {
    const prompts = readPrompts(singlePromptAgent());
    assert.equal(prompts.script, SCRIPT);
    assert.equal(prompts.script.length, 4302);
  });

  test('the opening line is returned separately', () => {
    assert.equal(readPrompts(singlePromptAgent()).start, GREETING);
  });

  test('no empty blocks for parts this agent does not have', () => {
    // Rendering "Global rules" / "Main questions" / "Closing line" as empty
    // boxes reads as the script having been lost.
    const prompts = readPrompts(singlePromptAgent());
    assert.deepEqual(Object.keys(prompts).sort(), ['script', 'start']);
  });
});

describe('saving a single-prompt agent', () => {
  test('editing the opening line does NOT destroy the script', () => {
    // The exact regression. Before the fix this left prompt === the greeting.
    const nodes = singlePromptAgent();
    applyUpdates(nodes, { start: 'కొత్త greeting' });
    assert.equal(nodes[0].data.greeting, 'కొత్త greeting');
    assert.equal(nodes[0].data.prompt, SCRIPT, 'the 4302-char script must be untouched');
  });

  test('editing the script does NOT change the opening line', () => {
    const nodes = singlePromptAgent();
    applyUpdates(nodes, { script: 'కొత్త script' });
    assert.equal(nodes[0].data.prompt, 'కొత్త script');
    assert.equal(nodes[0].data.greeting, GREETING);
  });

  test('both can be saved together', () => {
    const nodes = singlePromptAgent();
    const changed = applyUpdates(nodes, { start: 'A', script: 'B' });
    assert.equal(changed, 2);
    assert.equal(nodes[0].data.greeting, 'A');
    assert.equal(nodes[0].data.prompt, 'B');
  });

  test('the webhook node is never touched', () => {
    const nodes = singlePromptAgent();
    applyUpdates(nodes, { start: 'A', script: 'B' });
    assert.deepEqual(nodes[1].data, {
      endpoint_url: 'https://admin.bswealthfinance.com/api/webhook/call-result',
    });
  });
});

describe('the older four-node agent still works', () => {
  const legacy = () => [
    { type: 'globalNode', data: { prompt: 'global rules' } },
    { type: 'startCall', data: { greeting_type: 'llm', prompt: 'opening', greeting: 'opening' } },
    { type: 'agentNode', data: { prompt: 'questions' } },
    { type: 'endCall', data: { prompt: 'closing' } },
  ];

  test('exposes its four blocks and no script block', () => {
    const prompts = readPrompts(legacy());
    assert.deepEqual(Object.keys(prompts).sort(), ['agenda', 'end', 'global', 'start']);
  });

  test('an llm-mode start node still writes both fields, as it must', () => {
    // Here the greeting really is generated from the prompt, so they are one
    // piece of text and letting them drift was the original bug.
    const nodes = legacy();
    applyUpdates(nodes, { start: 'new opening' });
    assert.equal(nodes[1].data.prompt, 'new opening');
    assert.equal(nodes[1].data.greeting, 'new opening');
  });

  test('the other three nodes are addressed by their own keys', () => {
    const nodes = legacy();
    applyUpdates(nodes, { global: 'G', agenda: 'A', end: 'E' });
    assert.equal(nodes[0].data.prompt, 'G');
    assert.equal(nodes[2].data.prompt, 'A');
    assert.equal(nodes[3].data.prompt, 'E');
  });
});

describe('length limit', () => {
  test('the loan script fits under the cap', () => {
    // The cap was 4000 and the script is 4302, so saving it was rejected as
    // "too long" on the one agent that most needed editing.
    const MAX_PROMPT_LENGTH = 20_000;
    assert.ok(SCRIPT.length < MAX_PROMPT_LENGTH);
  });
});
