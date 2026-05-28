#!/bin/bash
# connect_claude.sh
# Ejecutar en macOS para exponer el VPS al sandbox de Claude.
# Uso: bash connect_claude.sh [stop]

set -e

VPS_USER="root"
VPS_HOST="2.25.141.45"
VPS="$VPS_USER@$VPS_HOST"

CLAUDE_PUBKEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII2uHai66n7UQz0mqFqeFOgwFKAsD0QXcnj2hC49RBVH scrapitero-cowork"

BORE_VERSION="v0.5.0"
BORE_URL="https://github.com/ekzhang/bore/releases/download/${BORE_VERSION}/bore-${BORE_VERSION}-x86_64-unknown-linux-musl.tar.gz"

# ── ControlMaster: una sola conexión SSH, sin pedir contraseña más de una vez
SOCKET="/tmp/claude_vps_tunnel.sock"
SSH="ssh -o ControlMaster=auto -o ControlPath=$SOCKET -o ControlPersist=120 -o StrictHostKeyChecking=no"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Scrapitero — Túnel Claude ↔ VPS       ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Modo stop ─────────────────────────────────────────────────────────────
if [ "${1}" = "stop" ]; then
  echo "▸ Cerrando túnel..."
  $SSH "$VPS" "pkill -f 'bore local' 2>/dev/null || true"
  ssh -O exit -o ControlPath=$SOCKET "$VPS" 2>/dev/null || true
  echo "✅ Túnel cerrado."
  exit 0
fi

# ── Abrir conexión maestra (pide contraseña UNA sola vez) ─────────────────
echo "▸ Conectando al VPS (pedirá la contraseña una sola vez)..."
$SSH "$VPS" "echo '  Conectado como: $(whoami)@$(hostname)'"

# ── 1. Agregar clave pública de Claude ────────────────────────────────────
echo "▸ [1/3] Instalando clave SSH de Claude..."
$SSH "$VPS" bash << 'ENDSSH'
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  grep -qF 'scrapitero-cowork' ~/.ssh/authorized_keys \
    || echo "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAII2uHai66n7UQz0mqFqeFOgwFKAsD0QXcnj2hC49RBVH scrapitero-cowork" \
       >> ~/.ssh/authorized_keys
  echo "  Clave OK"
ENDSSH

# ── 2. Instalar bore ──────────────────────────────────────────────────────
echo "▸ [2/3] Verificando bore en el VPS..."
BORE_URL_ESCAPED="$BORE_URL"
$SSH "$VPS" bash << ENDSSH
  if command -v bore > /dev/null 2>&1; then
    echo "  bore ya instalado: \$(bore --version 2>/dev/null || echo 'ok')"
  else
    echo "  Instalando bore..."
    curl -fsSL "$BORE_URL_ESCAPED" -o /tmp/bore.tar.gz
    tar xz -C /usr/local/bin -f /tmp/bore.tar.gz
    chmod +x /usr/local/bin/bore
    echo "  bore instalado OK"
  fi
ENDSSH

# ── 3. Iniciar túnel ──────────────────────────────────────────────────────
echo "▸ [3/3] Iniciando túnel bore.pub → VPS:22..."

$SSH "$VPS" bash << 'ENDSSH'
  pkill -f 'bore local' 2>/dev/null || true
  rm -f /tmp/bore_tunnel.log
  nohup bore local 22 --to bore.pub > /tmp/bore_tunnel.log 2>&1 &
ENDSSH

# Esperar hasta 15s a que bore publique el puerto
PORT=""
for i in $(seq 1 15); do
  sleep 1
  PORT=$($SSH "$VPS" "grep -oP 'bore\.pub:\K[0-9]+' /tmp/bore_tunnel.log 2>/dev/null | head -1" 2>/dev/null || true)
  if [ -n "$PORT" ]; then break; fi
  printf "  esperando bore... (%d/15)\r" "$i"
done

echo ""

if [ -z "$PORT" ]; then
  echo "❌ No se pudo obtener el puerto. Log:"
  $SSH "$VPS" "cat /tmp/bore_tunnel.log" || true
  exit 1
fi

# ── Resultado ─────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ✅ Túnel activo                         ║"
echo "╠══════════════════════════════════════════╣"
printf  "║  Endpoint:  bore.pub:%-20s║\n" "$PORT"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  Decile a Claude:"
echo "  → Conéctate al VPS en bore.pub:$PORT"
echo ""
echo "  Para cerrar cuando termines:"
echo "  → bash connect_claude.sh stop"
echo ""
