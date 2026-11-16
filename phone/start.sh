# Starts everything the game needs on the phone. Run it in Termux with:  sh start.sh
# It prints the two values to paste into Vercel, then keeps the tunnel open until Ctrl+C.
set -e
cd "$(dirname "$0")"

MODEL="${MODEL:-gemma3:1b}"
TOKEN_FILE="$HOME/.examiner_token"

for tool in python curl ollama cloudflared; do
  command -v "$tool" >/dev/null || { echo "Missing: $tool. Install it with: pkg install $tool"; exit 1; }
done

# Keep Android from putting Termux to sleep while the game is being played.
command -v termux-wake-lock >/dev/null && termux-wake-lock

# The secret is created once and reused, so Vercel only has to be told once.
if [ ! -s "$TOKEN_FILE" ]; then
  (umask 077; python -c 'import secrets; print(secrets.token_urlsafe(32))' > "$TOKEN_FILE")
fi
export PHONE_TOKEN="$(cat "$TOKEN_FILE")"
export MODEL

if ! curl -s -o /dev/null http://127.0.0.1:11434/api/version; then
  ollama serve >/dev/null 2>&1 &
  sleep 5
fi
ollama pull "$MODEL"

python relay.py &
RELAY=$!
trap 'kill "$RELAY" 2>/dev/null' EXIT INT TERM

echo
echo "PHONE_TOKEN for Vercel:  $PHONE_TOKEN"
echo "PHONE_URL for Vercel:    the https://....trycloudflare.com address shown below"
echo
cloudflared tunnel --url http://127.0.0.1:8787
