#!/bin/bash
# Instalador ultra-automatizado do hermesclawgowa (WAHA, compiladores, swap e skills)
# Foca a interatividade APENAS nas chaves de API. Todo o resto é automático.
set -euo pipefail

# Garante que o script execute a partir do diretório onde está localizado
cd "$(dirname "$0")"

# Se .env.example não existir no diretório atual, localiza ou clona o repositório
if [ ! -f .env.example ]; then
  if [ -d "/root/hermesclawgowa" ]; then
    cd "/root/hermesclawgowa"
  elif [ -d "$PWD/hermesclawgowa" ]; then
    cd "$PWD/hermesclawgowa"
  else
    if command -v git >/dev/null 2>&1; then
      git clone https://github.com/vendoideias2/hermesclawgowa.git /root/hermesclawgowa
      cd /root/hermesclawgowa
    fi
  fi
fi

# Cores
C_GREEN="$(printf '\033[32m')"
C_YELLOW="$(printf '\033[33m')"
C_RED="$(printf '\033[31m')"
C_BOLD="$(printf '\033[1m')"
C_OFF="$(printf '\033[0m')"

info()  { printf '%s[install]%s %s\n' "$C_GREEN" "$C_OFF" "$*"; }
warn()  { printf '%s[install]%s %s\n' "$C_YELLOW" "$C_OFF" "$*"; }
err()   { printf '%s[install]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; }
step()  { printf '\n%s==>%s %s%s%s\n' "$C_BOLD" "$C_OFF" "$C_BOLD" "$*" "$C_OFF"; }

as_root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"
  elif command -v sudo >/dev/null 2>&1; then sudo "$@"
  else err "Rode como root ou instale o sudo. Comando: $*"; return 1; fi
}

ask() {
  _p="$1"; _d="${2:-}"
  printf '%s [%s]: ' "$_p" "$_d" >/dev/tty
  IFS= read -r _a </dev/tty || _a=""
  [ -z "$_a" ] && _a="$_d"
  printf '%s' "$_a"
}

# 1. Detectar SO
step "Detectando Sistema Operacional"
OS="linux"
case "$(uname -s)" in
  Linux*)   OS="linux" ;;
  *)        err "Este instalador simplificado é otimizado para Linux/VPS."; exit 1 ;;
esac
info "Sistema detectado: Linux/VPS"

# 2. Instalação Automática das Ferramentas de Desenvolvimento Nativo no Host
step "Instalando compiladores e ferramentas nativas no host (Automático)"
as_root apt-get update -y
as_root apt-get install -y build-essential curl git wget jq unzip python3-pip python3-venv python3-dev ffmpeg

# Node.js LTS
if ! command -v node >/dev/null 2>&1; then
  info "Instalando Node.js LTS..."
  curl -fsSL https://deb.nodesource.com/setup_lts.x | as_root bash -
  as_root apt-get install -y nodejs
fi

# Go
if ! command -v go >/dev/null 2>&1; then
  info "Instalando Go compiler..."
  GO_VERSION=$(curl -s https://go.dev/VERSION?m=text | head -1)
  wget -q "https://go.dev/dl/${GO_VERSION}.linux-amd64.tar.gz" -O /tmp/go.tar.gz
  as_root rm -rf /usr/local/go && as_root tar -C /usr/local -xzf /tmp/go.tar.gz && rm /tmp/go.tar.gz
  if ! grep -q 'usr/local/go' ~/.bashrc; then
    echo 'export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin' >> ~/.bashrc
  fi
  export PATH=$PATH:/usr/local/go/bin:$HOME/go/bin
fi

# 3. Docker e Docker Compose
step "Garantindo Docker e Docker Compose (Automático)"
if ! command -v docker >/dev/null 2>&1; then
  info "Instalando Docker Engine..."
  curl -fsSL https://get.docker.com | sh
  usermod -aG docker root
fi
if ! docker compose version >/dev/null 2>&1; then
  as_root apt-get install -y docker-compose-plugin
fi
info "Docker: $(docker --version)"
info "Docker Compose: $(docker compose version)"

# 4. Swap de 12GB (Automático)
step "Configurando Swap de 12GB (Automático)"
swap_total=$(free -m | awk '/^Swap:/{print $2}')
if [ "${swap_total:-0}" -ge 12000 ]; then
  info "Swap de ${swap_total}MB já ativa. Pulando criação."
else
  if [ -f /swapfile ]; then
    as_root swapoff /swapfile || true
    as_root rm -f /swapfile
  fi
  info "Criando arquivo de swap de 12GB (aguarde...)"
  as_root fallocate -l 12G /swapfile || as_root dd if=/dev/zero of=/swapfile bs=1M count=12288 status=progress
  as_root chmod 600 /swapfile
  as_root mkswap /swapfile
  as_root swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab 2>/dev/null; then
    echo '/swapfile swap swap defaults 0 0' | as_root tee -a /etc/fstab >/dev/null
  fi
  as_root sysctl vm.swappiness=10
  echo 'vm.swappiness=10' | as_root tee -a /etc/sysctl.conf >/dev/null
  info "Swap de 12GB criada e ativada."
fi

# 5. Criando Arquivos de Configuração (.env e .env.secrets)
step "Configurando variáveis das APIs (.env)"
if [ ! -f .env ]; then
  cp .env.example .env
fi

# Funções auxiliares para leitura/escrita no .env
get_var() { grep "^$1=" .env | cut -d'=' -f2- | tr -d '"' || echo ""; }
set_var() {
  sed -i "/^$1=/d" .env
  echo "$1=\"$2\"" >> .env
}

# Define defaults de sistema silenciosamente
set_var DOMAIN "vendoideias.com"
set_var DEFAULT_MODEL "phi4:mini"
set_var WAHA_BASE_URL "http://waha:3000"
set_var WAHA_DEVICE_ID "default"

# Perguntas interativas — SOMENTE NAS APIS
info "Preencha as chaves de API correspondentes abaixo (pressione Enter para pular/manter padrão):"

# Meta Ads API
meta_token=$(ask "META_ACCESS_TOKEN (Meta Ads API Key)" "$(get_var META_ACCESS_TOKEN)")
set_var META_ACCESS_TOKEN "$meta_token"

meta_account=$(ask "META_AD_ACCOUNT_ID (Meta Ad Account ID, ex: act_1234)" "$(get_var META_AD_ACCOUNT_ID)")
set_var META_AD_ACCOUNT_ID "$meta_account"

# Backblaze B2 API
b2_key=$(ask "B2_KEY_ID (Backblaze Key ID)" "$(get_var B2_KEY_ID)")
set_var B2_KEY_ID "$b2_key"
b2_app=$(ask "B2_APP_KEY (Backblaze App Key)" "$(get_var B2_APP_KEY)")
set_var B2_APP_KEY "$b2_app"
b2_bucket=$(ask "B2_BUCKET (Backblaze Bucket Name)" "$(get_var B2_BUCKET)")
set_var B2_BUCKET "$b2_bucket"
b2_ep=$(ask "B2_ENDPOINT_URL (Backblaze Endpoint)" "$(get_var B2_ENDPOINT_URL)")
[ -z "$b2_ep" ] && b2_ep="https://s3.us-west-002.backblazeb2.com"
set_var B2_ENDPOINT_URL "$b2_ep"

# WAHA/WhatsApp API
waha_pass=$(ask "Senha do Dashboard do WAHA (Basic Auth)" "$(get_var WAHA_BASIC_AUTH)")
[ -z "$waha_pass" ] && waha_pass="admin:senha_forte_123"
set_var WAHA_BASIC_AUTH "$waha_pass"

wa_num=$(ask "WA_BRIDGE_ALLOWED_NUMBERS (Seu número com DDI/DDD para testes)" "$(get_var WA_BRIDGE_ALLOWED_NUMBERS)")
set_var WA_BRIDGE_ALLOWED_NUMBERS "$wa_num"

# 6. Gerando credenciais secretas (Automático)
step "Configurando Segredos (Automático)"
gen_secret() { python3 -c "import secrets; print(secrets.token_hex(32))"; }

for key in OPENCLAW_GATEWAY_TOKEN GOG_KEYRING_PASSWORD HERMES_API_SERVER_KEY WAHA_API_KEY; do
  val=$(get_var "$key")
  if [ -z "$val" ]; then
    set_var "$key" "$(gen_secret)"
  fi
done

# 7. Criando pastas de dados (Automático)
step "Criando diretórios de dados persistentes"
mkdir -p /root/.openclaw /root/.ollama /root/.hermes /root/.higgsfield /root/.lmstudio ./waha/sessions ./letsencrypt

# 8. Build
step "Construindo imagens Docker"
docker compose build

# 9. Auto-instalação de Skills (Automático)
step "Instalando Skills de IA na pasta de dados (Automático)"
mkdir -p /root/.openclaw/skills /root/.hermes/skills

install_sk() {
  local sk_name="$1"
  local sk_repo="https://github.com/${sk_name}.git"
  local dest="/root/.hermes/skills/$(basename "$sk_name")"
  if [ ! -d "$dest" ]; then
    git clone "$sk_repo" "$dest" || true
  fi
}
install_sk "codesstar/hermes-skill-atlas"
install_sk "prompt-security/clawsec"
install_sk "ChrisLamDev/hermes-core-skills"
install_sk "RobinBeraud/hermes-skills"
install_sk "izillionways/academic-research-skills-hermes"
info "Skills do catálogo Hermes/OpenClaw instaladas."

# 10. Configurar UFW (Automático - por último)
step "Configurando Firewall UFW (Automático)"
if command -v ufw >/dev/null 2>&1; then
  as_root ufw default deny incoming
  as_root ufw default allow outgoing
  as_root ufw allow 22/tcp comment 'SSH'
  as_root ufw allow 80/tcp comment 'HTTP'
  as_root ufw allow 443/tcp comment 'HTTPS'
  as_root ufw allow 18789/tcp comment 'OpenClaw UI'
  as_root ufw allow 3000/tcp comment 'WAHA API'
  as_root ufw allow 8642/tcp comment 'Hermes API'
  as_root ufw allow 9119/tcp comment 'Hermes Dashboard'
  echo "y" | as_root ufw enable
  info "UFW ativado e portas essenciais liberadas."
fi

step "Instalação finalizada com sucesso!"
info "Credenciais de API e Acesso geradas automaticamente (salvas em .env):"
echo "  OPENCLAW_GATEWAY_TOKEN  = $(get_var OPENCLAW_GATEWAY_TOKEN)"
echo "  HERMES_API_SERVER_KEY   = $(get_var HERMES_API_SERVER_KEY)"
echo "  WAHA_API_KEY            = $(get_var WAHA_API_KEY)"
echo ""
info "Você pode rodar 'docker compose up -d' para iniciar a stack com o WAHA."
