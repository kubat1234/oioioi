#!/bin/bash
# Updates the production server to a released version.
#
#   ./deploy.sh              shows the current version and recent releases, asks which one
#   ./deploy.sh v2026.11.12  deploys that release
#   ./deploy.sh --force …    deploys even while a round is running
#
# Steps: refuse during a running round → pull the image (site still up) →
# database backup → set SIO_TAG in .env → docker compose up (the migrate
# service runs migrations, web waits for it) → wait until web is healthy and
# the site answers. Rolling back: ./deploy.sh <previous version>; if the new
# version migrated the database, restore the backup printed below first.
#
# Backups go to SIO_BACKUP_DIR (default: $SIO_DATA_DIR/backups); only the
# newest SIO_BACKUP_KEEP (default 5) backups made by this script are kept.
# The script may be called through a symlink (e.g. ~/deploy.sh).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

REPO=kubat1234/oioioi
FORCE=0
VERSION=""
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) VERSION="$arg" ;;
    esac
done

[ -f .env ] || { echo "No .env next to docker-compose.yml (see .env.example)."; exit 1; }
env_value() { grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true; }
SIO_DATA_DIR=$(env_value SIO_DATA_DIR)
SIO_PORT=$(env_value SIO_PORT); SIO_PORT=${SIO_PORT:-8888}
CURRENT=$(env_value SIO_TAG); CURRENT=${CURRENT:-latest}
BACKUP_DIR=$(env_value SIO_BACKUP_DIR); BACKUP_DIR=${BACKUP_DIR:-${SIO_DATA_DIR:?set SIO_DATA_DIR in .env}/backups}
BACKUP_KEEP=$(env_value SIO_BACKUP_KEEP); BACKUP_KEEP=${BACKUP_KEEP:-5}

echo "Current version: $CURRENT"

if [ -z "$VERSION" ]; then
    echo "Recent releases (https://github.com/$REPO/releases):"
    releases=$(curl -fsS "https://api.github.com/repos/$REPO/releases?per_page=5" | grep -o '"tag_name": *"[^"]*"' | cut -d'"' -f4 || true)
    [ -n "$releases" ] || { echo "  (could not list releases; pass the version as an argument)"; exit 1; }
    echo "$releases" | sed 's/^/  /'
    newest=$(echo "$releases" | head -n1)
    read -r -p "Version to deploy [$newest]: " VERSION
    VERSION=${VERSION:-$newest}
fi

if [ "$VERSION" = "$CURRENT" ]; then
    read -r -p "$VERSION is already deployed. Deploy it again? [y/N] " again
    [ "$again" = "y" ] || exit 0
fi

# Rounds running right now (open-ended rounds without an end date do not count).
running=$(docker compose exec -T db psql -U oioioi -d oioioi -tA -c \
    "SELECT c.id || ': ' || r.name FROM contests_round r JOIN contests_contest c ON c.id = r.contest_id
     WHERE r.start_date <= now() AND r.end_date IS NOT NULL AND r.end_date > now();" 2>/dev/null || true)
if [ -n "$running" ]; then
    echo "Rounds running now:"; echo "$running" | sed 's/^/  /'
    if [ "$FORCE" != 1 ]; then
        echo "Refusing to deploy during a round (the site restarts). Use --force to override."
        exit 1
    fi
    echo "--force: deploying anyway."
fi

echo "==> Pulling $VERSION (the site keeps running)"
SIO_TAG="$VERSION" docker compose pull migrate web worker

echo "==> Database backup"
mkdir -p "$BACKUP_DIR"
backup="$BACKUP_DIR/oioioi-$(date +%Y%m%d-%H%M%S)-before-$VERSION.dump"
if ! docker compose exec -T db pg_dump -U oioioi -Fc oioioi > "$backup" || [ ! -s "$backup" ]; then
    rm -f "${backup:?}"
    echo "!! Backup failed, nothing changed."
    exit 1
fi
echo "    $backup ($(du -h "$backup" | cut -f1))"
# Only backups made by this script (oioioi-*-before-*.dump), newest first by name.
old=$(find "$BACKUP_DIR" -maxdepth 1 -name 'oioioi-*-before-*.dump' -printf '%f\n' | sort -r | tail -n +$((BACKUP_KEEP + 1)))
for f in $old; do
    rm -f "${BACKUP_DIR:?}/${f:?}"
    echo "    removed old backup $f"
done

echo "==> Switching .env to SIO_TAG=$VERSION"
if grep -qE '^SIO_TAG=' .env; then
    sed -i "s/^SIO_TAG=.*/SIO_TAG=$VERSION/" .env
else
    printf '\nSIO_TAG=%s\n' "$VERSION" >> .env
fi

echo "==> Starting (migrations first)"
if ! docker compose up -d --remove-orphans; then
    echo "!! docker compose up failed. Migration log:"
    docker compose logs --no-log-prefix --tail 50 migrate || true
    echo "Roll back: restore the backup, then ./deploy.sh $CURRENT"
    echo "  docker compose exec -T db pg_restore -U oioioi -d oioioi --clean --if-exists < $backup"
    exit 1
fi

echo "==> Waiting for the site"
for _ in $(seq 1 60); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q web)" 2>/dev/null || echo starting)
    if [ "$status" = healthy ] && curl -fsS -o /dev/null "http://localhost:$SIO_PORT/"; then
        echo "OK: $VERSION is running (was $CURRENT)."
        echo "Backup from before: $backup"
        exit 0
    fi
    sleep 5
done
echo "!! The site did not come up within 5 minutes. Check: docker compose logs web"
echo "Roll back: ./deploy.sh $CURRENT (and restore $backup if migrations ran)"
exit 1
