#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state_dir=${TENDIES_DATA_DIR:-/home/jason/.local/share/tendies}
deploy_base=${TENDIES_DEPLOY_BASE:-/home/jason/.local/share/deployer/tendies}
deployer_bin=${TENDIES_DEPLOYER_BIN:-deployer}
secrets_file=${TENDIES_SECRETS_FILE:-$repo/.env.prod}

run_check() {
    command -v uv >/dev/null 2>&1 || {
        echo "pipeline: uv is required for frozen dependency checks" >&2
        return 1
    }
    (cd "$repo" && uv sync --frozen --dev && uv run --frozen python -m pytest)
}

write_source_revision() {
    allow_dirty=${1:-no}
    rm -f "$repo/.source-revision"
    if test "$allow_dirty" != yes && test -n "$(git -C "$repo" status --porcelain)"; then
        echo "pipeline: production deploy requires a clean committed checkout (pass --allow-dirty explicitly to override)" >&2
        return 1
    fi
    revision=$(git -C "$repo" rev-parse HEAD)
    if test "$allow_dirty" = yes && test -n "$(git -C "$repo" status --porcelain)"; then
        revision="$revision-dirty"
    fi
    printf '%s\n' "$revision" > "$repo/.source-revision"
}

command=${1:-check}
if test $# -gt 0; then shift; fi

case "$command" in
    check|cloud-check)
        run_check
        ;;
    deploy)
        allow_dirty=no
        if test "${1:-}" = "--allow-dirty"; then allow_dirty=yes; shift; fi
        run_check
        write_source_revision "$allow_dirty"
        trap 'rm -f "$repo/.source-revision"' EXIT HUP INT TERM
        "$deployer_bin" deploy "$repo" --env "$secrets_file" "$@"
        ;;
    deploy-dry-run)
        "$deployer_bin" deploy "$repo" --dry-run --env "$secrets_file"
        ;;
    local-deploy)
        test -r "$secrets_file" || { echo "pipeline: production secrets file is not readable" >&2; exit 1; }
        exec /usr/bin/python3 "$repo/scripts/local_deploy.py" "$repo" --env "$secrets_file"
        ;;
    status|rollback|restart|stop)
        exec "$deployer_bin" "$command" "$repo" "$@"
        ;;
    logs)
        exec "$deployer_bin" logs "$repo" "$@"
        ;;
    backup-local)
        reason=${1:-manual}
        exec /usr/bin/python3 "$repo/src/tendies/ops.py" backup --reason "$reason"
        ;;
    backup)
        reason=${1:-manual}
        case "$reason" in *[!A-Za-z0-9_-]*|'') echo "pipeline: invalid backup reason" >&2; exit 2;; esac
        exec ssh desktop "/usr/bin/python3 /home/jason/.local/share/deployer/tendies/current/src/tendies/ops.py backup --reason $reason"
        ;;
    backup-fetch)
        destination=${1:-$repo/.backups}
        mkdir -p "$destination"
        remote=$(ssh desktop "/usr/bin/python3 /home/jason/.local/share/deployer/tendies/current/src/tendies/ops.py backup --reason manual")
        case "$remote" in
            /home/jason/.local/share/tendies/backups/tendies-*.sqlite3) ;;
            *) echo "pipeline: target returned an invalid backup path" >&2; exit 1;;
        esac
        scp "desktop:$remote" "$destination/"
        chmod 600 "$destination/$(basename "$remote")"
        ;;
    restore-local)
        backup=${1:-}
        confirm=${2:-}
        test -n "$backup" && test "$confirm" = "--confirm" || {
            echo "usage: scripts/pipeline.sh restore-local BACKUP --confirm" >&2
            exit 2
        }
        if systemctl --user is-active --quiet deployer-tendies.service; then
            echo "pipeline: stop deployer-tendies.service before restoring SQLite" >&2
            exit 1
        fi
        exec /usr/bin/python3 "$repo/src/tendies/ops.py" restore "$backup"
        ;;
    poll-main)
        source_repo=${TENDIES_SOURCE_REPO:-$state_dir/source}
        secrets_file=${TENDIES_SECRETS_FILE:-/home/jason/.local/share/deployer/tendies/.deployer/environment}
        mkdir -p "$state_dir/candidates"
        exec 9>"$state_dir/poll.lock"
        flock -n 9 || exit 0
        git -C "$source_repo" fetch --prune origin main
        revision=$(git -C "$source_repo" rev-parse refs/remotes/origin/main)
        current_revision=
        if test -r "$deploy_base/current/.source-revision"; then
            current_revision=$(sed -n '1p' "$deploy_base/current/.source-revision")
        fi
        test "$revision" != "$current_revision" || exit 0
        candidate=$(mktemp -d "$state_dir/candidates/main.XXXXXX")
        rmdir "$candidate"
        cleanup() {
            git -C "$source_repo" worktree remove --force "$candidate" >/dev/null 2>&1 || true
        }
        trap cleanup EXIT HUP INT TERM
        git -C "$source_repo" worktree add --detach "$candidate" "$revision"
        printf '%s\n' "$revision" > "$candidate/.source-revision"
        (cd "$candidate" && scripts/pipeline.sh check)
        (cd "$candidate" && TENDIES_SECRETS_FILE="$secrets_file" scripts/pipeline.sh local-deploy)
        ;;
    install-timers)
        unit_dir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
        mkdir -p "$unit_dir"
        cp "$repo/scripts/systemd/tendies-poll.service" "$unit_dir/"
        cp "$repo/scripts/systemd/tendies-poll.timer" "$unit_dir/"
        cp "$repo/scripts/systemd/tendies-backup.service" "$unit_dir/"
        cp "$repo/scripts/systemd/tendies-backup.timer" "$unit_dir/"
        systemctl --user daemon-reload
        systemctl --user enable --now tendies-poll.timer tendies-backup.timer
        ;;
    *)
        echo "usage: scripts/pipeline.sh {check|cloud-check|deploy|deploy-dry-run|local-deploy|status|logs|rollback|restart|stop|backup|backup-fetch|backup-local|restore-local|poll-main|install-timers}" >&2
        exit 2
        ;;
esac
