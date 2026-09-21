/* Real project bridge against a fake Cordis context; no DSH/model call. */
import assert from 'node:assert/strict';
import http from 'node:http';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const events = [], requests = [], callbacks = new Map();
let registration = null, cancelled = 0;
const sink = http.createServer(async (request, response) => {
  let raw = ''; for await (const part of request) raw += part;
  const body = JSON.parse(raw || '{}');
  let output = {ok: true};
  if (request.url === '/register') registration = body;
  if (request.url === '/events') events.push(...body);
  if (request.url === '/request') {
    requests.push(body);
    output = body.kind === 'approval'
      ? {decision: 'allow'}
      : {answers: body.questions.map(question => ({id: question.id, selected: [], custom: 'ok'}))};
  }
  response.setHeader('Content-Type', 'application/json');
  response.end(JSON.stringify(output));
});
await new Promise(resolveReady => sink.listen(0, '127.0.0.1', resolveReady));
process.env.SUPPLY_CHAIN_DSH_BRIDGE_URL = 'http://127.0.0.1:' + sink.address().port;
process.env.SUPPLY_CHAIN_APPROVAL_MODE = 'auto';
const context = {
  on: (kind, callback) => callbacks.set(kind, callback),
  approval: {setPolicy: () => {}}, agents: {}, userQuestions: {},
};
const {apply} = await import(pathToFileURL(resolve('dsh_runtime/bridge.mjs')));
await apply(context);
const agent = {id: 'session-1', cancel: () => { cancelled += 1; }};
callbacks.get('agent/created')({agent});
callbacks.get('agent/assistant-stream')({agent, frame: {type: 'chunk', chunk: {type: 'text-delta', text: 'visible'}}});
callbacks.get('agent/assistant-stream')({agent, frame: {type: 'chunk', chunk: {type: 'reasoning-delta', text: 'private'}}});
await new Promise(resolveWait => setTimeout(resolveWait, 90));
assert.equal(registration.stream, true);
assert(events.some(event => event.kind === 'assistant.stream'));
assert(!JSON.stringify(events).includes('private'));
let result = await callbacks.get('approval/request')({agent, toolName: 'pwsh', callId: 'call-1', reason: 'test'}, () => {});
assert.equal(result, 'allowed-once');
assert.equal(requests.length, 0);
process.env.SUPPLY_CHAIN_APPROVAL_MODE = 'ask';
result = await callbacks.get('approval/request')({agent, toolName: 'pwsh', callId: 'call-2', reason: 'test'}, () => {});
assert.equal(result, 'allowed-once');
assert.equal(requests[0].call_id, 'call-2');
result = await callbacks.get('user-questions/request')({agent, questions: [{id: 'q1', question: 'test'}]}, () => {});
assert.equal(result.answers[0].id, 'q1');
await fetch('http://127.0.0.1:' + registration.control_port + '/cancel', {method: 'POST', body: JSON.stringify({session_id: 'session-1'})});
assert.equal(cancelled, 1);
callbacks.get('dispose')();
await new Promise(resolveWait => setTimeout(resolveWait, 60));
sink.close();
console.log(JSON.stringify({ok: true, checks: 7, scope: 'bridge contract only'}));

