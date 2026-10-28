// Managed OpenCode HTTP bridge: all session events, including children, are visible.
import {spawn} from 'node:child_process';
import {TRUSTED_PERMISSION, toolEvent} from './protocol.js';
import {runAsyncTurn} from './async-turn.js';

const input = JSON.parse(await new Promise((resolve, reject) => {
  let text = '';
  process.stdin.on('data', chunk => {text += chunk;});
  process.stdin.on('end', () => resolve(text));
  process.stdin.on('error', reject);
}));
const permission = input.conversation
  ? [{permission: '*', pattern: '*', action: 'deny'}] : TRUSTED_PERMISSION;
const server = spawn('opencode', ['serve', '--port', '0', '--hostname', '127.0.0.1'], {
  cwd: input.directory, stdio: ['ignore', 'pipe', 'pipe'],
});
let address;
let root;
const family = new Set();
const controller = new AbortController();
const emit = (type, data = {}) => process.stdout.write(JSON.stringify({type, sessionID: root, ...data}) + '\n');

async function shutdown() {
  controller.abort();
  server.kill('SIGTERM');
}
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
try {
  address = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('OpenCode server readiness timeout')), 30000);
    let text = '';
    server.stdout.on('data', chunk => {
      text += chunk;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {clearTimeout(timer); resolve(match[0]);}
    });
    server.stderr.on('data', () => {});
    server.on('exit', code => {clearTimeout(timer); reject(new Error(`OpenCode server exited ${code}`));});
  });
  async function request(route, method = 'GET', body) {
    const headers = {'content-type': 'application/json', 'x-opencode-directory': input.directory};
    if (process.env.OPENCODE_SERVER_PASSWORD) {
      headers.authorization = 'Basic ' + Buffer.from(`${process.env.OPENCODE_SERVER_USERNAME || 'opencode'}:${process.env.OPENCODE_SERVER_PASSWORD}`).toString('base64');
    }
    const response = await fetch(address + route, {method, headers,
      body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
    if (!response.ok) {
      if (response.status === 404 && route === '/session/' + input.sessionID) throw new Error('Session not found');
      throw new Error(`OpenCode HTTP ${response.status} on ${route}`);
    }
    return response;
  }
  if (input.sessionID) {
    const response = await request('/session/' + input.sessionID);
    root = (await response.json()).id;
    await request('/session/' + root, 'PATCH', {permission});
  } else {
    root = (await (await request('/session', 'POST', {permission})).json()).id;
  }
  family.add(root);
  async function addChildren(sessionID) {
    const children = await (await request(`/session/${sessionID}/children`)).json();
    for (const child of children) {
      if (family.has(child.id)) continue;
      family.add(child.id);
      await addChildren(child.id);
    }
  }
  if (input.sessionID) await addChildren(root);
  emit('session_start');
  const stream = await request('/event');
  const completedParts = new Set();
  const startedParts = new Set();
  let failure;
  const consume = (async () => {
    let buffer = '';
    const decoder = new TextDecoder();
    for await (const chunk of stream.body) {
      buffer += decoder.decode(chunk, {stream: true});
      let end;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const raw = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trim()).join('\n');
        if (!raw) continue;
        const event = JSON.parse(raw), p = event.properties || {};
        if (event.type === 'session.created' && family.has(p.info?.parentID)) family.add(p.info.id);
        if (event.type === 'permission.asked' && family.has(p.sessionID)) {
          await request(`/session/${p.sessionID}/permissions/${p.id}`, 'POST', {response: input.conversation ? 'reject' : 'always'});
          emit('permission_resolved', {sourceSessionID: p.sessionID});
        }
        if (event.type === 'session.error' && family.has(p.sessionID)) {
          failure = p.error; emit('error', {error: p.error, sourceSessionID: p.sessionID});
        }
        if (event.type === 'message.part.updated' && family.has(p.part?.sessionID)) {
          const part = p.part;
          if (part.type === 'tool') {
            const child = part.state.metadata?.sessionId;
            if (child) family.add(child);
            const update = toolEvent(part, startedParts, completedParts);
            if (update) emit(update.type, {part: update.part, sourceSessionID: part.sessionID});
          }
          if (part.type === 'step-finish') emit('step_finish', {part, sourceSessionID: part.sessionID});
        }
        if (event.type === 'session.status' && family.has(p.sessionID)) emit('session_status', {status: p.status, sourceSessionID: p.sessionID});
      }
    }
  })().catch(error => {if (!controller.signal.aborted) failure = {message: error.message};});
  const [providerID, ...model] = input.model.split('/');
   const history = await runAsyncTurn(request, root, {
    model: {providerID, modelID: model.join('/')}, variant: input.variant,
     ...(input.utility || input.conversation ? {tools: {read:false, write:false, edit:false, apply_patch:false,
      bash:false, task:false, grep:false, glob:false, webfetch:false, skill:false}} : {}),
    agent: input.agent || 'build', parts: [{type: 'text', text: input.prompt}],
   }, {failure: () => failure});
   for (const message of history) {
     if (message.info.role !== 'assistant') continue;
    for (const part of message.parts) if (part.type === 'text') emit('text', {part});
  }
  if (failure) throw new Error('OpenCode session failed');
  controller.abort();
  await consume;
} catch (error) {
   emit('error', {error: {message: error.message, cause: error.cause?.code || error.cause?.message}});
  process.exitCode = 1;
} finally {
  await shutdown();
  await new Promise(resolve => {
    if (server.exitCode !== null) return resolve();
    const timer = setTimeout(() => {server.kill('SIGKILL'); resolve();}, 3000);
    server.once('exit', () => {clearTimeout(timer); resolve();});
  });
}
