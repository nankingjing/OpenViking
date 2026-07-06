#!/usr/bin/env bash
#
# OpenViking Memory Plugin shared installer for Claude Code and Codex.
#
# One-liner:
#   bash <(curl -fsSL https://raw.githubusercontent.com/volcengine/OpenViking/main/examples/memory-plugin-shared/install.sh)
#
# Non-interactive:
#   bash install.sh --harness claude,codex --url http://127.0.0.1:1933 --api-key ''
#
# Targets bash 3.2+ (macOS /bin/bash) and Linux.

set -euo pipefail

OV_HOME="${OPENVIKING_HOME:-$HOME/.openviking}"
REPO_URL="${OPENVIKING_REPO_URL:-https://github.com/volcengine/OpenViking.git}"
REPO_DIR="${OPENVIKING_REPO_DIR:-$OV_HOME/openviking-repo}"
REPO_REF="${OPENVIKING_REPO_REF:-${OPENVIKING_REPO_BRANCH:-main}}"
REPO_ARCHIVE_URL="${OPENVIKING_REPO_ARCHIVE_URL:-}"
ARCHIVE_MARKER='.openviking-archive-source'
OVCLI_CONF="${OPENVIKING_CLI_CONFIG_FILE:-$OV_HOME/ovcli.conf}"

CODEX_MARKETPLACE_NAME="${OPENVIKING_CODEX_MARKETPLACE_NAME:-openviking-plugins-local}"
CODEX_MARKETPLACE_ROOT="${OPENVIKING_CODEX_MARKETPLACE_ROOT:-$HOME/.codex/${CODEX_MARKETPLACE_NAME}-marketplace}"
CODEX_PLUGIN_NAME="openviking-memory"
CODEX_PLUGIN_ID="${CODEX_PLUGIN_NAME}@${CODEX_MARKETPLACE_NAME}"
CODEX_CONFIG="${CODEX_CONFIG_FILE:-$HOME/.codex/config.toml}"

CLAUDE_MARKETPLACE_NAME="${OPENVIKING_CLAUDE_MARKETPLACE_NAME:-openviking-plugins-local}"
CLAUDE_PLUGIN_NAME="openviking-memory"
CLAUDE_PLUGIN_ID="${CLAUDE_PLUGIN_NAME}@${CLAUDE_MARKETPLACE_NAME}"
CLAUDE_LEGACY_PLUGIN_ID="claude-code-memory-plugin@${CLAUDE_MARKETPLACE_NAME}"

REQUESTED_HARNESSES=""
URL_ARG=""
API_KEY_ARG="__OPENVIKING_UNSET__"
ACCOUNT_ARG="__OPENVIKING_UNSET__"
USER_ARG="__OPENVIKING_UNSET__"
YES=0
SOURCE_IS_CURRENT=0

if [ -t 1 ]; then
  CYAN=$'\033[0;36m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  CYAN=''; GREEN=''; YELLOW=''; RED=''; BOLD=''; RESET=''
fi
info()    { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn()    { printf '%s!!%s  %s\n' "$YELLOW" "$RESET" "$*"; }
err()     { printf '%sxx%s  %s\n' "$RED" "$RESET" "$*" >&2; }
ask()     { printf '%s??%s  %s' "$CYAN" "$RESET" "$*"; }
heading() { printf '\n%s%s%s\n' "$BOLD" "$*" "$RESET"; }

usage() {
  cat <<EOF
Usage: install.sh [--harness claude,codex] [--url URL] [--api-key KEY] [--account ACCOUNT] [--user USER] [--yes]

Options:
  --harness LIST   Comma-separated harnesses: claude, codex, or both.
  --url URL        OpenViking server base URL.
  --api-key KEY    OpenViking API key. Pass '' for unauthenticated local mode.
  --account ID     Optional OpenViking account.
  --user ID        Optional OpenViking user.
  --yes            Use defaults for prompts when possible.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --harness) REQUESTED_HARNESSES="${2:-}"; shift 2 ;;
    --url) URL_ARG="${2:-}"; shift 2 ;;
    --api-key) API_KEY_ARG="${2-}"; shift 2 ;;
    --account) ACCOUNT_ARG="${2-}"; shift 2 ;;
    --user) USER_ARG="${2-}"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    --help|-h) usage; exit 0 ;;
    *) err "Unknown argument: $1"; usage; exit 2 ;;
  esac
done

split_harnesses() {
  printf '%s\n' "$1" | tr ',' '\n' | while IFS= read -r h; do
    h=$(printf '%s' "$h" | tr '[:upper:]' '[:lower:]' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    [ -n "$h" ] && printf '%s\n' "$h"
  done
}

contains_harness() {
  local want="$1" h
  while IFS= read -r h; do
    [ "$h" = "$want" ] && return 0
  done <<EOF
$(split_harnesses "$SELECTED_HARNESSES")
EOF
  return 1
}

json_get() {
  local file="$1" key="$2"
  [ -f "$file" ] || return 0
  node -e '
    try {
      const c = JSON.parse(require("node:fs").readFileSync(process.argv[1], "utf8"));
      const v = c[process.argv[2]];
      if (v != null && v !== "") process.stdout.write(String(v));
    } catch {}
  ' "$file" "$key" 2>/dev/null || true
}

json_merge_ovcli() {
  local file="$1" url="$2" key="$3" account="$4" user="$5"
  node - "$file" "$url" "$key" "$account" "$user" <<'NODE'
const fs = require("node:fs");
const [file, url, apiKey, account, user] = process.argv.slice(2);
let c = {};
try { c = JSON.parse(fs.readFileSync(file, "utf8")); } catch {}
if (url) c.url = url;
if (apiKey !== "__OPENVIKING_KEEP__") c.api_key = apiKey;
if (account !== "__OPENVIKING_KEEP__") {
  if (account) c.account = account; else delete c.account;
}
if (user !== "__OPENVIKING_KEEP__") {
  if (user) c.user = user; else delete c.user;
}
fs.mkdirSync(require("node:path").dirname(file), { recursive: true });
fs.writeFileSync(file, JSON.stringify(c, null, 2) + "\n", { mode: 0o600 });
NODE
  chmod 600 "$file" 2>/dev/null || true
}

fetch_archive() {
  local url="$1" dest="$2" tmp_zip tmp_dir top
  command -v unzip >/dev/null 2>&1 || { err 'unzip not found; required to install from an archive.'; exit 1; }
  tmp_zip=$(mktemp "${TMPDIR:-/tmp}/ov-src.XXXXXX") || { err 'mktemp failed'; exit 1; }
  tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ov-src.XXXXXX") || { err 'mktemp failed'; rm -f "$tmp_zip"; exit 1; }
  info "Downloading source archive"
  info "  $url"
  curl -fsSL -o "$tmp_zip" "$url" || { err "download failed: $url"; rm -rf "$tmp_zip" "$tmp_dir"; exit 1; }
  unzip -q "$tmp_zip" -d "$tmp_dir" || { err 'unzip failed'; rm -rf "$tmp_zip" "$tmp_dir"; exit 1; }
  top=$(find "$tmp_dir" -mindepth 1 -maxdepth 1 -type d | head -n 1)
  if [ -z "$top" ] || [ ! -d "$top/examples" ]; then
    err 'unexpected archive layout (no top-level dir containing examples/)'
    rm -rf "$tmp_zip" "$tmp_dir"; exit 1
  fi
  rm -rf "$dest"
  mkdir -p "$(dirname "$dest")"
  mv "$top" "$dest"
  : > "$dest/$ARCHIVE_MARKER"
  rm -rf "$tmp_zip" "$tmp_dir"
}

resolve_self_repo() {
  local src dir
  src="${BASH_SOURCE[0]}"
  dir="$(cd "$(dirname "$src")" >/dev/null 2>&1 && pwd -P)"
  if [ -d "$dir/../../.git" ]; then
    REPO_DIR="$(cd "$dir/../.." >/dev/null 2>&1 && pwd -P)"
    SOURCE_IS_CURRENT=1
    info "Using current checkout: $REPO_DIR"
  fi
}

select_harnesses() {
  local detected="" reply default
  command -v claude >/dev/null 2>&1 && detected="${detected:+$detected,}claude"
  command -v codex >/dev/null 2>&1 && detected="${detected:+$detected,}codex"

  if [ -n "$REQUESTED_HARNESSES" ]; then
    SELECTED_HARNESSES="$REQUESTED_HARNESSES"
    return
  fi
  default="${detected:-claude,codex}"
  if [ -t 0 ] && [ "$YES" -ne 1 ]; then
    info "Detected harnesses: ${detected:-none}"
    ask "Install harnesses [${default}]: "
    read -r reply || reply=""
    SELECTED_HARNESSES="${reply:-$default}"
  else
    SELECTED_HARNESSES="$default"
  fi
}

validate_selected_harnesses() {
  local h bad=0
  while IFS= read -r h; do
    case "$h" in
      claude|codex) ;;
      *) err "Unsupported harness: $h"; bad=1 ;;
    esac
  done <<EOF
$(split_harnesses "$SELECTED_HARNESSES")
EOF
  [ "$bad" -eq 0 ] || exit 2
}

configure_ovcli() {
  local current_url current_key current_account current_user url key account user reply mode url_input
  heading "2. OpenViking client config ($OVCLI_CONF)"
  mkdir -p "$OV_HOME"
  chmod 700 "$OV_HOME" 2>/dev/null || true

  current_url="$(json_get "$OVCLI_CONF" url)"
  current_key="$(json_get "$OVCLI_CONF" api_key)"
  current_account="$(json_get "$OVCLI_CONF" account)"
  current_user="$(json_get "$OVCLI_CONF" user)"

  url="$current_url"
  key="__OPENVIKING_KEEP__"
  account="__OPENVIKING_KEEP__"
  user="__OPENVIKING_KEEP__"

  [ -n "$URL_ARG" ] && url="$URL_ARG"
  [ "$API_KEY_ARG" != "__OPENVIKING_UNSET__" ] && key="$API_KEY_ARG"
  [ "$ACCOUNT_ARG" != "__OPENVIKING_UNSET__" ] && account="$ACCOUNT_ARG"
  [ "$USER_ARG" != "__OPENVIKING_UNSET__" ] && user="$USER_ARG"

  if [ -z "$url" ] && [ -t 0 ] && [ "$YES" -ne 1 ]; then
    printf '%sChoose where you will connect to OpenViking:%s\n' "$BOLD" "$RESET"
    printf '  1) Self-hosted / local                          [default: http://127.0.0.1:1933]\n'
    printf '  2) Volcengine OpenViking Cloud                  [https://api.vikingdb.cn-beijing.volces.com/openviking]\n'
    ask '[1/2, default 1]: '
    read -r mode || mode=""
    case "$mode" in
      2) url="https://api.vikingdb.cn-beijing.volces.com/openviking" ;;
      *)
        ask 'OpenViking server URL [http://127.0.0.1:1933]: '
        read -r url_input || url_input=""
        url="${url_input:-http://127.0.0.1:1933}"
        ;;
    esac
  fi
  [ -z "$url" ] && url="${current_url:-http://127.0.0.1:1933}"

  if [ "$key" = "__OPENVIKING_KEEP__" ] && [ -z "$current_url" ] && [ -t 0 ] && [ "$YES" -ne 1 ]; then
    ask 'API key (leave empty for unauthenticated local mode): '
    if read -rs reply 2>/dev/null; then printf '\n'; else read -r reply || reply=""; fi
    key="$reply"
  fi

  if [ "$account" = "__OPENVIKING_KEEP__" ] && [ "$ACCOUNT_ARG" = "__OPENVIKING_UNSET__" ]; then
    account="__OPENVIKING_KEEP__"
  fi
  if [ "$user" = "__OPENVIKING_KEEP__" ] && [ "$USER_ARG" = "__OPENVIKING_UNSET__" ]; then
    user="__OPENVIKING_KEEP__"
  fi

  if [ -f "$OVCLI_CONF" ]; then
    cp "$OVCLI_CONF" "$OVCLI_CONF.bak.$(date +%s)"
  fi
  json_merge_ovcli "$OVCLI_CONF" "$url" "$key" "$account" "$user"
  info "Config ready: $OVCLI_CONF"
}

prepare_source() {
  heading "3. OpenViking source ($REPO_DIR)"
  resolve_self_repo
  if [ "$SOURCE_IS_CURRENT" -eq 1 ]; then
    info "Using current checkout without modifying git state."
  elif [ -n "$REPO_ARCHIVE_URL" ]; then
    if [ -e "$REPO_DIR" ] && [ ! -f "$REPO_DIR/$ARCHIVE_MARKER" ] && [ ! -d "$REPO_DIR/.git" ]; then
      err "$REPO_DIR exists and is not an OpenViking checkout/archive."
      exit 1
    fi
    fetch_archive "$REPO_ARCHIVE_URL" "$REPO_DIR"
  elif [ -d "$REPO_DIR/.git" ]; then
    info "Refreshing checkout ($REPO_REF)"
    git -C "$REPO_DIR" fetch --depth 1 origin "$REPO_REF"
    git -C "$REPO_DIR" reset --hard FETCH_HEAD
  else
    if [ -e "$REPO_DIR" ]; then
      err "$REPO_DIR exists but is not a git checkout."
      exit 1
    fi
    info "Cloning $REPO_URL (ref $REPO_REF)"
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"
  fi
}

strip_rc_block() {
  local rc="$1" begin="$2" end="$3"
  [ -n "$rc" ] && [ -f "$rc" ] || return 0
  grep -qF "$begin" "$rc" || return 0
  if ! grep -qF "$end" "$rc"; then
    warn "Found $begin in $rc but missing end marker; leaving it untouched."
    return 0
  fi
  awk -v b="$begin" -v e="$end" '
    $0 == b {skip=1; next}
    $0 == e {skip=0; next}
    !skip
  ' "$rc" > "$rc.tmp" && mv "$rc.tmp" "$rc"
  info "Removed legacy rc block from $rc"
}

cleanup_rc_wrappers() {
  local rc
  for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    strip_rc_block "$rc" '# >>> openviking claude-code memory plugin >>>' '# <<< openviking claude-code memory plugin <<<'
    strip_rc_block "$rc" '# >>> openviking-codex-plugin >>>' '# <<< openviking-codex-plugin <<<'
  done
}

install_claude() {
  heading "4. Claude Code plugin"
  if ! command -v claude >/dev/null 2>&1; then
    warn "claude CLI not found; skipping Claude Code install."
    return 0
  fi
  if ! command claude plugin --help >/dev/null 2>&1; then
    warn "This Claude Code build lacks 'claude plugin'; legacy install remains available via the old setup-helper if needed."
    return 0
  fi
  if command claude plugin marketplace list 2>/dev/null | grep -qF "$CLAUDE_MARKETPLACE_NAME"; then
    command claude plugin marketplace update "$CLAUDE_MARKETPLACE_NAME" >/dev/null 2>&1 || true
  else
    ( cd "$REPO_DIR" && command claude plugin marketplace add "$REPO_DIR/examples" ) >/dev/null 2>&1 || true
  fi
  command claude plugin uninstall "$CLAUDE_LEGACY_PLUGIN_ID" >/dev/null 2>&1 || true
  if command claude plugin list 2>/dev/null | grep -qF "$CLAUDE_PLUGIN_ID"; then
    ( cd "$REPO_DIR" && command claude plugin update "$CLAUDE_PLUGIN_ID" ) || warn "claude plugin update returned non-zero"
  else
    ( cd "$REPO_DIR" && command claude plugin install "$CLAUDE_PLUGIN_ID" ) || warn "claude plugin install returned non-zero"
  fi
  command claude plugin enable "$CLAUDE_PLUGIN_ID" >/dev/null 2>&1 || true
  info "Claude plugin requested: $CLAUDE_PLUGIN_ID"
}

ensure_codex_config() {
  node - "$CODEX_CONFIG" "$CODEX_PLUGIN_ID" <<'NODE'
const fs = require("node:fs");
const path = process.argv[2];
const pluginId = process.argv[3];
let text = "";
try { text = fs.readFileSync(path, "utf8"); } catch {}
function ensureSectionLine(src, section, key, value) {
  const lines = src.split(/\n/);
  const header = `[${section}]`;
  const start = lines.findIndex((line) => line.trim() === header);
  if (start === -1) {
    const prefix = src.trimEnd();
    return `${prefix}${prefix ? "\n\n" : ""}${header}\n${key} = ${value}\n`;
  }
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) if (/^\s*\[/.test(lines[i])) { end = i; break; }
  for (let i = start + 1; i < end; i += 1) {
    if (new RegExp(`^\\s*${key}\\s*=`).test(lines[i])) {
      lines[i] = `${key} = ${value}`;
      return lines.join("\n").replace(/\n*$/, "\n");
    }
  }
  lines.splice(end, 0, `${key} = ${value}`);
  return lines.join("\n").replace(/\n*$/, "\n");
}
function ensurePluginEnabled(src, pluginId) {
  const header = `[plugins."${pluginId}"]`;
  const lines = src.split(/\n/);
  const start = lines.findIndex((line) => line.trim() === header);
  if (start === -1) {
    const prefix = src.trimEnd();
    return `${prefix}${prefix ? "\n\n" : ""}${header}\nenabled = true\n`;
  }
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) if (/^\s*\[/.test(lines[i])) { end = i; break; }
  for (let i = start + 1; i < end; i += 1) {
    if (/^\s*enabled\s*=/.test(lines[i])) {
      lines[i] = "enabled = true";
      return lines.join("\n").replace(/\n*$/, "\n");
    }
  }
  lines.splice(end, 0, "enabled = true");
  return lines.join("\n").replace(/\n*$/, "\n");
}
text = ensurePluginEnabled(text, pluginId);
text = ensureSectionLine(text, "features", "plugin_hooks", "true");
fs.mkdirSync(require("node:path").dirname(path), { recursive: true });
fs.writeFileSync(path, text);
NODE
}

install_codex() {
  heading "4. Codex plugin"
  if ! command -v codex >/dev/null 2>&1; then
    warn "codex CLI not found; skipping Codex install."
    return 0
  fi
  mkdir -p "$CODEX_MARKETPLACE_ROOT/.agents/plugins" "$HOME/.codex"
  rm -f "$CODEX_MARKETPLACE_ROOT/$CODEX_PLUGIN_NAME"
  ln -s "$REPO_DIR/examples/codex-memory-plugin" "$CODEX_MARKETPLACE_ROOT/$CODEX_PLUGIN_NAME"
  cat > "$CODEX_MARKETPLACE_ROOT/marketplace.json" <<EOF
{
  "name": "$CODEX_MARKETPLACE_NAME",
  "plugins": [
    {
      "name": "$CODEX_PLUGIN_NAME",
      "source": "./$CODEX_PLUGIN_NAME",
      "policy": { "installation": "AVAILABLE", "authentication": "ON_USE" },
      "category": "Productivity"
    }
  ]
}
EOF
  cp "$CODEX_MARKETPLACE_ROOT/marketplace.json" "$CODEX_MARKETPLACE_ROOT/.agents/plugins/marketplace.json"
  command codex plugin marketplace add "$CODEX_MARKETPLACE_ROOT" >/dev/null 2>&1 || true
  if ! command codex plugin add "$CODEX_PLUGIN_ID" >/dev/null 2>&1; then
    command codex plugin install "$CODEX_PLUGIN_ID" >/dev/null 2>&1 || warn "codex plugin add/install returned non-zero for $CODEX_PLUGIN_ID from $CODEX_MARKETPLACE_ROOT; config was still updated"
  fi
  ensure_codex_config
  info "Codex plugin enabled in $CODEX_CONFIG"
}

validate_stdio_configs() {
  heading "5. Validation"
  local ok=1
  if contains_harness claude; then
    node --test "$REPO_DIR/examples/claude-code-memory-plugin/scripts/marketplace.test.mjs" || ok=0
  fi
  if contains_harness codex; then
    node --test "$REPO_DIR/examples/codex-memory-plugin/scripts/marketplace.test.mjs" || ok=0
    node --check "$REPO_DIR/examples/codex-memory-plugin/servers/mcp-proxy.mjs" || ok=0
  fi
  node --check "$REPO_DIR/examples/claude-code-memory-plugin/servers/mcp-proxy.mjs" || ok=0
  [ "$ok" -eq 1 ] || exit 1
}

heading "1. Environment check"
case "$(uname -s)" in
  Darwin|Linux) info "OS: $(uname -s)" ;;
  *) err "Unsupported OS: $(uname -s). Only macOS and Linux are supported."; exit 1 ;;
esac
command -v node >/dev/null 2>&1 || { err "node not found. Install Node.js 18+."; exit 1; }
NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
[ "$NODE_MAJOR" -ge 18 ] || { err "Node.js 18+ required; found $(node --version)."; exit 1; }
if [ -z "$REPO_ARCHIVE_URL" ]; then
  command -v git >/dev/null 2>&1 || { err "git not found."; exit 1; }
fi
command -v curl >/dev/null 2>&1 || warn "curl not found; archive installs may fail."

select_harnesses
validate_selected_harnesses
info "Selected harnesses: $(printf '%s' "$SELECTED_HARNESSES" | tr ',' ' ')"

configure_ovcli
prepare_source
cleanup_rc_wrappers

if contains_harness claude; then install_claude; fi
if contains_harness codex; then install_codex; fi
validate_stdio_configs

heading "Done"
info "Source: $REPO_DIR"
info "Config: $OVCLI_CONF"
info "MCP: stdio proxy reads ovcli.conf/env at runtime"
if contains_harness claude; then info "Claude: $CLAUDE_PLUGIN_ID"; fi
if contains_harness codex; then info "Codex:  $CODEX_PLUGIN_ID"; fi
