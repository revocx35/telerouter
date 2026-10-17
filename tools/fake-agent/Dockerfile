# Disposable SSH host with tmux and a fake "claude" CLI that fires Claude Code-style hooks.
# Used by tools/e2e_agent.py to test telerouter's SSH/tmux/hook/MCP plumbing without a real agent.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server tmux python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* && mkdir -p /run/sshd \
 && useradd -m -s /bin/bash agent \
 && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
COPY fake_claude.py /usr/local/bin/claude
RUN chmod 755 /usr/local/bin/claude
COPY entrypoint.sh /entrypoint.sh
CMD ["sh", "/entrypoint.sh"]
