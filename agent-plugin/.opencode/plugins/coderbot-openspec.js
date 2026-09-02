import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const bridgeSkillsDir = path.resolve(__dirname, '../../skills');

export const CoderbotOpenSpecPlugin = async () => ({
  config: async (config) => {
    config.skills = config.skills || {};
    config.skills.paths = config.skills.paths || [];
    if (!config.skills.paths.includes(bridgeSkillsDir)) {
      config.skills.paths.push(bridgeSkillsDir);
    }
  },
});
