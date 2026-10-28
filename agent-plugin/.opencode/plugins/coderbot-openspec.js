import path from 'path';
import { fileURLToPath } from 'url';
import { trustedConfig, trustedSessionPolicy } from '../../runtime/protocol.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bridgeSkillsDir = path.resolve(__dirname, '../../skills');

export const CoderbotOpenSpecPlugin = async (input) => {
  const policy = input?.serverUrl ? trustedSessionPolicy(input) : null;
  const limit = Number(process.env.CODEBOT_MAX_SUBAGENTS || 3);
  const queueTimeout = Number(process.env.CODEBOT_LOCAL_TOOL_TIMEOUT || 200) * 1000;
  if (!Number.isInteger(limit) || limit < 1 || !Number.isFinite(queueTimeout) || queueTimeout <= 0) {
    throw new Error('Subagent concurrency and queue timeout must be positive');
  }
  let active = 0;
  const waiters = [];
  const assignments = new Set();
  function release(callID) {
    if (!assignments.delete(callID)) return;
    active--;
    const waiter = waiters.shift();
    if (waiter) {
      clearTimeout(waiter.timer);
      // Reserve the freed slot before waking the waiter; new calls cannot steal it.
      active++;
      assignments.add(waiter.callID);
      waiter.resolve();
    }
  }
  return {
  ...(policy ? {
    event: async (payload) => {
      const event = payload.event;
      const part = event.properties?.part;
      // OpenCode may skip tool.execute.after when execution throws.
      if (event.type === 'message.part.updated' && part?.type === 'tool' &&
          ['completed', 'error'].includes(part.state?.status)) release(part.callID);
      await policy.event(payload);
    },
    'chat.message': policy.before,
    'tool.execute.before': async (call, output) => {
      await policy.before(call);
      if (call.tool !== 'task') return;
      if (await policy.isChild(call.sessionID)) {
        throw new Error('Nested subagent delegation is disabled. Complete the work directly; do not retry the task tool.');
      }
      if (active >= limit) {
        await new Promise((resolve, reject) => {
          const waiter = {callID: call.callID, resolve};
          waiter.timer = setTimeout(() => {
            waiters.splice(waiters.indexOf(waiter), 1);
            reject(new Error('Subagent queue timeout. Complete the work directly; do not retry the task tool.'));
          }, queueTimeout);
          waiters.push(waiter);
        });
        return;
      }
      active++;
      assignments.add(call.callID);
    },
    'tool.execute.after': async (call) => {
      release(call.callID);
    },
  } : {}),
  config: async (config) => {
    trustedConfig(config);
    config.skills = config.skills || {};
    config.skills.paths = config.skills.paths || [];
    if (!config.skills.paths.includes(bridgeSkillsDir)) {
      config.skills.paths.push(bridgeSkillsDir);
    }
  },
  };
};
