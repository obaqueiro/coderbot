FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client curl ca-certificates gnupg util-linux ffmpeg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Target repos' e2e/run.sh installs its own pinned Playwright npm package and browser
# binary at runtime, as the non-root `bot` user (into a user-writable cache dir) — but
# launching headless Chromium also needs a set of OS shared libraries that require
# root/apt to install, which `bot` doesn't have at runtime. Install those once here,
# as root, at build time. `install-deps` only touches OS packages (not the browser
# binary itself), so it isn't tied to any particular target repo's Playwright version.
RUN npx --yes playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code opencode-ai@1.18.18 @fission-ai/openspec@1.9.0 \
    && npm install --prefix /opt/coderbot/plugins \
        "superpowers@git+https://github.com/obra/superpowers.git#v6.3.0"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# claude refuses --dangerously-skip-permissions as root; uid 501 matches the
# macOS host user so the mounted repo and ~/.claude stay writable.
RUN useradd -u 501 -m -s /bin/bash bot

COPY agent-plugin /opt/coderbot/agent-plugin
RUN claude plugin validate \
        /opt/coderbot/plugins/node_modules/superpowers/.claude-plugin/plugin.json \
    && claude plugin validate /opt/coderbot/agent-plugin/.claude-plugin/plugin.json \
    && node --check /opt/coderbot/agent-plugin/.opencode/plugins/coderbot-openspec.js \
    && node -e 'const p = require("/opt/coderbot/agent-plugin/package.json"); \
        if (p.main !== ".opencode/plugins/coderbot-openspec.js") process.exit(1)'

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
