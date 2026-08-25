#!/usr/bin/env bash
#
# Step 1 of 2 — copy this project onto your machine.
#
#   ./get-repo.sh
#
# Downloads the repository into a folder in your home directory and tells you where it
# put it. Nothing is built and nothing is started; that is scripts/setup-demo.sh, which
# you run next and which lives inside the folder this script creates.
#
# Re-running is safe: an existing copy is updated instead of downloaded again.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Tech-Ahir/stripe-collections-agent.git}"
DEST="${DEST:-$HOME/stripe-collections-agent}"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mStopped:\033[0m %s\n\n' "$1" >&2; exit 1; }

bold "Getting the Stripe Collections Agent"
echo

command -v git >/dev/null 2>&1 || die \
  "git is not installed.

  On a Mac, open Terminal and run:   xcode-select --install
  Then run this script again."
ok "git is installed"

if [ -d "$DEST/.git" ]; then
  ok "Already downloaded — updating your copy"
  git -C "$DEST" pull --ff-only \
    || echo "  (kept your existing copy; it has local changes)"
else
  # The repository is PRIVATE. Cloning needs a GitHub account that has been granted
  # access. Anything else here is almost always an access problem, not a typo.
  if ! git clone "$REPO_URL" "$DEST"; then
    die "Could not download the repository.

  It is private, so GitHub has to know who you are and your account has to
  have been given access.

  The simplest fix, one time only:

    1. Install the GitHub CLI:   brew install gh
    2. Sign in:                  gh auth login
    3. Run this script again.

  If you have access and it still fails, ask for an invitation to the
  repository to be re-sent."
  fi
  ok "Downloaded"
fi

echo
bold "Done. Your copy is here:"
echo
echo "    $DEST"
echo
cat <<EOF
  Two things live in there that you will want:

    knowledge-base/     the Obsidian vault — open this folder as a vault
    scripts/setup-demo.sh   step 2, which builds and starts the demo

  Next:

    cd "$DEST"
    ./scripts/setup-demo.sh

EOF
