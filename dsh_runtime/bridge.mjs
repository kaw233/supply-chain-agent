/* DSH remains the agent. This plugin only bridges stream, approval and questions. */
import http from 'node:http';

export const name = 'supply-chain-stream-and-interaction';
export const inject = ['agents', 'approval', 'userQuestions'];

export async function apply(ctx) {
  const sink = process.env.SUPPLY_CHAIN_DSH_BRIDGE_URL;
  if (!sink) throw new Error('SUPPLY_CHAIN_DSH_BRIDGE_URL missing');
  const agents = new Map();
  let queue = [], tail = Promise.resolve(), closed = false, sequence = 0, lastError = null;
  const send = (path, data, signal) => fetch(sink + path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data), signal,
  }).then(async response => {
    if (!response.ok) throw new Error('DSH bridge HTTP ' + response.status);
    return response.json();
  });
  const emit = (kind, data) => { if (!closed) queue.push({kind, data: {...data, bridge_seq: ++sequence}}); };
  const flush = () => {
    if (!queue.length) return tail;
    const batch = queue; queue = [];
    tail = tail.then(() => send('/events', batch)).catch(error => {
      lastError = error;
      process.stderr.write('DSH bridge event error: ' + error.message + '\n');
    });
    return tail;
  };
  const timer = setInterval(flush, 35); timer.unref();
  const sessionId = agent => agent?.session?.id ?? agent?.sessionId ?? agent?.id;

  ctx.on('agent/created', ({agent}) => {
    agents.set(sessionId(agent), agent);
    try { ctx.approval.setPolicy(agent, 'ask'); } catch (_) {}
    emit('agent.created', {session_id: sessionId(agent)});
  });
  ctx.on('agent/assistant-stream', ({agent, frame}) => {
    if (frame?.type === 'chunk' && frame.chunk?.type === 'reasoning-delta') return;
    emit('assistant.stream', {session_id: sessionId(agent), frame});
  });
  ctx.on('approval/request', async (request, next) => {
    if (!agents.has(sessionId(request.agent))) return next();
    if (request.signal?.aborted) return 'cancelled';
    if (process.env.SUPPLY_CHAIN_APPROVAL_MODE === 'auto') {
      emit('approval.automatic', {
        session_id: sessionId(request.agent), tool: request.toolName,
        call_id: request.callId, scope: 'project-workspace',
      });
      return 'allowed-once';
    }
    await flush();
    try {
      const answer = await send('/request', {
        kind: 'approval', session_id: sessionId(request.agent), tool: request.toolName,
        call_id: request.callId, reason: request.reason,
      }, request.signal);
      return answer.decision === 'allow' ? 'allowed-once' : 'rejected';
    } catch (_) {
      return request.signal?.aborted ? 'cancelled' : 'unavailable';
    }
  });
  ctx.on('user-questions/request', async (request, next) => {
    if (request.agent && !agents.has(sessionId(request.agent))) return next();
    await flush();
    return send('/request', {
      kind: 'questions', session_id: sessionId(request.agent), questions: request.questions,
    }, request.signal);
  });

  const control = http.createServer(async (request, response) => {
    try {
      let raw = ''; for await (const part of request) raw += part;
      const body = raw ? JSON.parse(raw) : {};
      if (request.url === '/flush') {
        await flush();
        response.writeHead(lastError ? 502 : 200, {'Content-Type': 'application/json'});
        response.end(JSON.stringify({ok: !lastError, error: lastError?.message}));
      } else if (request.url === '/cancel') {
        const agent = agents.get(body.session_id);
        if (agent) agent.cancel({kind: 'user'}, {keepInbox: false});
        response.writeHead(200, {'Content-Type': 'application/json'});
        response.end(JSON.stringify({requested: !!agent}));
      } else {
        response.writeHead(404); response.end('{}');
      }
    } catch (error) {
      response.writeHead(500); response.end(JSON.stringify({error: String(error)}));
    }
  });
  await new Promise(resolve => control.listen(0, '127.0.0.1', resolve));
  await send('/register', {
    control_port: control.address().port, profile: 'sdk', stream: true,
    questions: true, approval: true, version: 1,
  });
  ctx.on('dispose', () => { clearInterval(timer); flush(); closed = true; control.close(); });
}

export default {name, inject, apply};

