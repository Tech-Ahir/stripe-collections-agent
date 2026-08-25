#!/usr/bin/env bash
#
# Step 2 of 2 — build and start the demo.
#
#   ./scripts/setup-demo.sh
#
# Asks you for three values, builds the two Docker images, starts the system and checks
# that it came up correctly. Takes about five minutes, most of it the first build.
#
# Re-running is safe. An existing .env is left exactly as it is.

set -euo pipefail

cd "$(dirname "$0")/.."

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '\n\033[31mStopped:\033[0m %s\n\n' "$1" >&2; exit 1; }

REUSED_STANDARD_KEY=0

# ----------------------------------------------------------------------------------
bold "1 of 5   Checking what you have installed"
# ----------------------------------------------------------------------------------

command -v docker >/dev/null 2>&1 || die \
  "Docker Desktop is not installed.

  Download it free from:  https://www.docker.com/products/docker-desktop/
  Install it, open it, wait for the whale icon in your menu bar to stop
  animating, then run this script again."
ok "Docker Desktop is installed"

# Compose v2 is a docker subcommand. The legacy standalone docker-compose is not enough:
# this project uses --wait, profiles and depends_on.condition, none of which v1 has.
docker compose version >/dev/null 2>&1 || die \
  "Your Docker is too old — it does not have Compose v2.

  Open Docker Desktop and let it update itself, or reinstall from
  https://www.docker.com/products/docker-desktop/"
ok "Docker Compose $(docker compose version --short)"

docker info >/dev/null 2>&1 || die \
  "Docker Desktop is installed but not running.

  Open Docker Desktop from your Applications folder. Wait for the whale
  icon in your menu bar to stop animating, then run this script again."
ok "Docker Desktop is running"

# ----------------------------------------------------------------------------------
echo
bold "2 of 5   Settings"
# ----------------------------------------------------------------------------------

if [ -f .env ]; then
  ok "You already have a .env — leaving it alone"
else
  [ -f .env.demo ] || die "Cannot find .env.demo. Are you inside the project folder?"
  cp .env.demo .env
  chmod 600 .env

  # 32 bytes of hex. Shared by the two services and nothing else; it is what makes an
  # approval token unforgeable. Generated per install so no two copies share one.
  if command -v openssl >/dev/null 2>&1; then
    SECRET="$(openssl rand -hex 32)"
  elif command -v python3 >/dev/null 2>&1; then
    SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  else
    die "Need either openssl or python3 to generate a signing secret."
  fi
  ok "Generated a signing secret"

  echo
  echo "  Three values, then everything else is automatic."
  echo

  echo "  1. Your Anthropic API key — this runs the agent."
  echo "     Get one at https://console.anthropic.com  ->  API keys"
  echo "     Press Enter to skip; the approval flow still works without it."
  printf '     Anthropic key (sk-ant-...): '
  read -r ANTHROPIC_KEY

  echo
  echo "  2. The Stripe read key, from the message that invited you to this"
  echo "     repository. Starts with rk_test_"
  printf '     Stripe read key: '
  read -r STRIPE_READ
  [ -n "$STRIPE_READ" ] || die \
    "The Stripe read key is required.

  It was sent to you with your repository invitation. Look for the two
  lines beginning STRIPE_API_KEY_READ and STRIPE_API_KEY_SEED."

  echo
  echo "  3. The Stripe seed key, from the same message. Starts with sk_test_"
  printf '     Stripe seed key: '
  read -r STRIPE_SEED
  [ -n "$STRIPE_SEED" ] || die "The Stripe seed key is required. It was in the same message."

  case "$STRIPE_READ" in
    rk_*) : ;;
    *) REUSED_STANDARD_KEY=1 ;;
  esac

  # sed on macOS needs the empty -i argument; GNU sed does not accept it. Write with
  # python3 where available so this behaves the same on both.
  if command -v python3 >/dev/null 2>&1; then
    ANTHROPIC_KEY="$ANTHROPIC_KEY" STRIPE_READ="$STRIPE_READ" \
    STRIPE_SEED="$STRIPE_SEED" SECRET="$SECRET" python3 - <<'PY'
import os, pathlib, re
path = pathlib.Path(".env")
text = path.read_text()
for name in ("ANTHROPIC_API_KEY", "STRIPE_API_KEY_READ", "STRIPE_API_KEY_SEED", "APPROVAL_SIGNING_SECRET"):
    value = {
        "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_KEY"],
        "STRIPE_API_KEY_READ": os.environ["STRIPE_READ"],
        "STRIPE_API_KEY_SEED": os.environ["STRIPE_SEED"],
        "APPROVAL_SIGNING_SECRET": os.environ["SECRET"],
    }[name]
    text = re.sub(rf"(?m)^{name}=.*$", f"{name}={value}", text, count=1)
path.write_text(text)
PY
  else
    sed -i.bak \
      -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${ANTHROPIC_KEY}|" \
      -e "s|^STRIPE_API_KEY_READ=.*|STRIPE_API_KEY_READ=${STRIPE_READ}|" \
      -e "s|^STRIPE_API_KEY_SEED=.*|STRIPE_API_KEY_SEED=${STRIPE_SEED}|" \
      -e "s|^APPROVAL_SIGNING_SECRET=.*|APPROVAL_SIGNING_SECRET=${SECRET}|" .env
    rm -f .env.bak
  fi
  ok "Wrote your settings to .env"
fi

# ----------------------------------------------------------------------------------
echo
bold "3 of 5   Building"
# ----------------------------------------------------------------------------------
echo "  The first build takes a few minutes. Later ones are much faster."
echo

docker compose up -d --build --wait --wait-timeout 300

ok "app is running at http://localhost:8000"
ok "gateway is running, reachable only from inside Docker"

# ----------------------------------------------------------------------------------
echo
bold "4 of 5   Checking it works"
# ----------------------------------------------------------------------------------

curl -fsS http://localhost:8000/healthz >/dev/null 2>&1 \
  || die "The app did not answer. See what it said:  docker compose logs app"
ok "The dashboard answers"

# The most important property of the whole system: the gateway publishes no port, so
# nothing outside Docker can ask it to act. If this ever succeeds, the boundary is gone.
if curl -sS --max-time 5 http://localhost:9000/healthz >/dev/null 2>&1; then
  die "The gateway answered from outside Docker. It must not.
  Someone has added a ports: entry to the gateway in docker-compose.yml."
fi
ok "The gateway refuses connections from outside Docker, as designed"

HEALTH="$(curl -fsS http://localhost:8000/healthz 2>/dev/null || echo '{}')"
case "$HEALTH" in
  *'"stripe": {"status": "unauthorized"'*|*'"status": "unauthorized"'*)
    warn "Stripe rejected the read key. Check you pasted it correctly, then run:"
    warn "    docker compose up -d --force-recreate app" ;;
  *'"key_kind": "standard"'*) REUSED_STANDARD_KEY=1 ;;
esac

# ----------------------------------------------------------------------------------
echo
bold "5 of 5   Loading the demo data"
# ----------------------------------------------------------------------------------
echo "  Six customers and eight invoices, from 3 to 95 days overdue."
echo

docker compose run --rm seed scripts/seed_stripe_test_data.py --recreate

# ----------------------------------------------------------------------------------
echo
if [ "$REUSED_STANDARD_KEY" = "1" ]; then
  bold "Ready — with one note."
  echo
  warn "The read key is not a restricted one, so the dashboard shows an amber"
  warn "'read key is not restricted' badge. The demo works; the badge is telling"
  warn "you the read-only split is not in force. Check you used the rk_test_ key."
else
  bold "Ready."
fi
cat <<'EOF'

  Open this in your browser:

      http://localhost:8000

  Press "Start agent run" and watch the agent work. When it finishes, go to
  "To review" and approve a letter.

  To see the whole approval boundary proved in a terminal, in one minute:

      docker compose exec app python scripts/demo_boundary.py

  To stop everything:

      docker compose down

EOF
