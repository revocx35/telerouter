#!/bin/sh
mkdir -p /home/agent/.ssh
printf '%s\n' "$AUTHORIZED_KEY" > /home/agent/.ssh/authorized_keys
chown -R agent:agent /home/agent/.ssh && chmod 700 /home/agent/.ssh && chmod 600 /home/agent/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e -p 22
