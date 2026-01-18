#!/usr/bin/env bash
set -euo pipefail

repo="https://github.com/RobertBiehl/ai-convos-db.git"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
install_dir="${CONVOS_INSTALL_DIR:-$HOME/.local/share/ai-convos-db}"
bin_dir="${CONVOS_BIN_DIR:-$HOME/.local/bin}"
uv_bin="$HOME/.local/share/uv/tools/ai-convos-db/bin"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$repo_root/.uv-cache}"

ensure_path() {
  local shell_rc=""
  if [ -n "${ZSH_VERSION-}" ]; then shell_rc="$HOME/.zshrc"
  elif [ -n "${BASH_VERSION-}" ]; then shell_rc="$HOME/.bashrc"
  else shell_rc="$HOME/.profile"; fi
  if ! grep -q "$bin_dir" "$shell_rc" 2>/dev/null; then
    printf '\nexport PATH="%s:$PATH"\n' "$bin_dir" >> "$shell_rc"
  fi
}

if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi

mkdir -p "$install_dir" "$bin_dir"
if [ -f "$repo_root/pyproject.toml" ] && [ -d "$repo_root/.git" ]; then
  install_dir="$repo_root"
else
  if [ ! -d "$install_dir/.git" ]; then
    git clone "$repo" "$install_dir"
  else
    git -C "$install_dir" pull --ff-only
  fi
fi

UV_NO_CACHE=1 uv tool install "$install_dir" --force
ln -sf "$uv_bin/convos" "$bin_dir/convos"
ensure_path
if [ "${CONVOS_INSTALL_SKILLS:-1}" = "1" ]; then "$bin_dir/convos" install-skills || true; fi
echo "Installed convos to $bin_dir/convos"
echo "Restart your shell or run: export PATH=\"$bin_dir:\$PATH\""
