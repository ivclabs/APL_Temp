#!/bin/bash
# Update the server to the newest code on GitHub. Run ON the server:
#   sudo /opt/apl-temp/infra/update.sh
set -euo pipefail
cd /opt/apl-temp
git pull --ff-only
PLAYBOOK=$(command -v ansible-playbook || echo /usr/local/bin/ansible-playbook)
$PLAYBOOK infra/ansible/playbook.yml
systemctl restart apl-temp    # always load the new code
echo "Updated to: $(git log --oneline -1)"
