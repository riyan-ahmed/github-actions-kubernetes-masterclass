
#!/bin/bash
set -euo pipefail

mkdir -p backups

DATE=$(date +%Y%m%d-%H%M%S)
FILE="backups/skillpulse-$DATE.sql"

echo "Backing up SkillPulse MySQL..."

kubectl exec -n skillpulse-prd mysql-0 -- \
sh -c '
  umask 077
  printf "[client]\nuser=%s\npassword=%s\n" "$MYSQL_USER" "$MYSQL_PASSWORD" > /tmp/skillpulse-backup.cnf
  trap "rm -f /tmp/skillpulse-backup.cnf" EXIT
  mysqldump --defaults-extra-file=/tmp/skillpulse-backup.cnf --single-transaction --no-tablespaces "$MYSQL_DATABASE"
' > "$FILE"

echo "Backup created: $FILE"
