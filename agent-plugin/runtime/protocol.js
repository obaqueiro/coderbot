// OpenCode 1.18.18 HTTP/event contract. No private runtime modules required.
export const RUNTIME_VERSION = '1.18.18';
export const TRUSTED_PERMISSION = [{ permission: '*', pattern: '*', action: 'allow' }];

export function trustedConfig(config) {
  config.permission = {'*': 'allow'};
  config.agent ||= {};
  for (const name of new Set(['build', 'plan', 'general', 'explore', ...Object.keys(config.agent)])) {
    config.agent[name] = { ...config.agent[name], permission: {'*': 'allow'} };
  }
  return config;
}

export function permissionRequest(event) {
  if (event?.type !== 'permission.asked') return null;
  const p = event.properties;
  if (!p?.id || !p?.sessionID) throw new Error('Invalid permission.asked event');
  return { sessionID: p.sessionID, permissionID: p.id, response: 'always' };
}

export function toolEvent(part, started, completed) {
  const state = part.state || {};
  const identity = part.id || `${part.sessionID}:${part.callID}`;
  if (completed.has(identity)) return null;
  const done = ['completed', 'error'].includes(state.status);
  if (done) completed.add(identity);
  else if (state.status === 'running' && (!started.has(identity) || state.metadata?.sessionId)) {
    started.add(identity);
  } else return null;
  return {type: done ? 'tool_use' : 'tool_start', part: {...part, id: identity}};
}

export function trustedSessionPolicy({serverUrl, directory, fetch: request = globalThis.fetch}) {
  const prepared = new Set();
  const parents = new Map();
  async function send(route, method, body) {
    const headers = {'content-type': 'application/json', 'x-opencode-directory': directory};
    if (process.env.OPENCODE_SERVER_PASSWORD) {
      headers.authorization = 'Basic ' + Buffer.from(`${process.env.OPENCODE_SERVER_USERNAME || 'opencode'}:${process.env.OPENCODE_SERVER_PASSWORD}`).toString('base64');
    }
    const result = await request(new URL(route, serverUrl), {
      method,
      headers,
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
    if (!result.ok) throw new Error(`Trusted runtime request failed: ${method} ${result.status}`);
    return result;
  }
  async function prepare(sessionID) {
    if (prepared.has(sessionID)) return;
    await send(`/session/${encodeURIComponent(sessionID)}`, 'PATCH', {permission: TRUSTED_PERMISSION});
    prepared.add(sessionID);
  }
  return {
    prepare,
    async isChild(sessionID) {
      if (!parents.has(sessionID)) {
        const response = await send(`/session/${encodeURIComponent(sessionID)}`, 'GET');
        const info = await response.json();
        parents.set(sessionID, info.parentID || null);
      }
      return Boolean(parents.get(sessionID));
    },
    async event({event}) {
      const permission = permissionRequest(event);
      if (permission) {
        await send(`/session/${encodeURIComponent(permission.sessionID)}/permissions/${encodeURIComponent(permission.permissionID)}`,
          'POST', {response: permission.response});
        return;
      }
      if (event.type === 'session.created') {
        const info = event.properties.info;
        parents.set(info.id, info.parentID || null);
        await prepare(info.id);
      }
    },
    async before(input) { await prepare(input.sessionID); },
  };
}
