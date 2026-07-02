# 🗺️ GUIA DEFINITIVO — Instalação Completa da Stack HermesClawGowa na VPS
## (WAHA/GOWA, Traefik, HTTPS, Compiladores Nativos, Modelos e Skills)

Este guia cobre a preparação completa do sistema e da infraestrutura da sua VPS (8GB de RAM), a instalação dos compiladores nativos, a configuração de swap de 12GB, o provisionamento de LLMs no Ollama e a orquestração do sistema de agentes com o catálogo de skills do Hermes/OpenClaw.

---

## 📋 Arquitetura de Rede e Roteamento (Traefik + HTTPS)

Toda a comunicação externa da VPS será criptografada automaticamente (Let's Encrypt SSL) através do **Traefik** como proxy reverso, encaminhando para os seguintes subdomínios:

*   **`open.vendoideias.com`** ➜ Roteia para o OpenClaw UI (Porta `18789`)
*   **`hermes.vendoideias.com`** ➜ Roteia para o Hermes Dashboard (Porta `9119`)

---

## 🚀 PASSO 1: Apontamento de DNS

Antes de iniciar na VPS, acesse o painel do seu registrador de domínio (`vendoideias.com`) e adicione dois registros do **Tipo A** apontando para o IP público da sua VPS:

| Nome do Host | Tipo | Destino (IP) |
| :--- | :--- | :--- |
| `open.vendoideias.com` | A | `IP_DA_SUA_VPS` |
| `hermes.vendoideias.com` | A | `IP_DA_SUA_VPS` |

---

## 🚀 PASSO 2: Linha de Comando de Instalação Unificada (One-Liner)

Conecte-se na sua VPS via SSH e cole o comando completo abaixo. Ele realizará a preparação do sistema, instalação de compiladores e inicialização interativa da stack:

```bash
cd /root && git clone https://github.com/vendoideias2/hermesclawgowa.git && cd hermesclawgowa && chmod +x install.sh && ./install.sh
```

---

## 🚀 PASSO 3: O que o script `install.sh` executa de forma sequencial

### 3.1 Instalação de Compiladores e Ferramentas Nativas (Host)
O instalador instalará diretamente no host da VPS todas as dependências necessárias para desenvolvimento, deploy e execução de scripts de skills:
*   **Node.js LTS** (via repositório NodeSource oficial)
*   **Go Compiler** (instalado em `/usr/local/go` e configurado no PATH)
*   **Python 3, venv, pip, python3-dev** (para isolamento e execução de skills de IA)
*   **Ferramentas Úteis:** `build-essential`, `curl`, `wget`, `jq`, `unzip`, `ffmpeg` (processamento de áudio/vídeo).

### 3.2 Partição Swap de 12GB
Para evitar travamentos de falta de memória (OOM) ao carregar os modelos locais em uma VPS de 8GB de RAM, o script cria e ativa um arquivo `/swapfile` fixo de **12GB**, otimizando a swappiness do kernel Linux para `10`.

### 3.3 Docker Engine & Compose v2
Garante o Docker configurado para inicialização automática no boot do sistema e o plugin `docker-compose-plugin` v2 atualizado.

### 3.4 Firewall UFW (Segurança Nativa)
Configura e ativa o firewall nativo do Linux permitindo o tráfego apenas nas portas públicas do projeto:
*   `22/tcp` (SSH)
*   `80/tcp` & `443/tcp` (Traefik HTTP/HTTPS Let's Encrypt)
*   `3000/tcp` (WhatsApp API - WAHA/GOWA)
*   `18789/tcp` (OpenClaw UI)
*   `8642/tcp` (Hermes API Gateway)
*   `9119/tcp` (Hermes Dashboard)

### 3.5 Escolha Interativa da API do WhatsApp
Durante a execução, você escolherá de forma interativa qual API deseja utilizar:
*   **[1] WAHA (Recomendado):** WhatsApp HTTP API leve, sem banco de dados local Postgres (economiza RAM), persistindo sessões em arquivo JSON em `./waha/sessions`.
*   **[2] GOWA (Legado):** Go WhatsApp Multidevice tradicional, reativando os containers extras de banco de dados `postgres:15-alpine` para persistência das credenciais.

O script copiará automaticamente o template correto (`docker-compose.waha.yml` ou `docker-compose.gowa.yml`) para o arquivo final `docker-compose.yml`.

### 3.6 Instalação Automática do Catálogo de Skills
Clona e ativa as melhores skills do ecossistema do Hermes e OpenClaw diretamente no diretório de dados persistentes do agente:
*   `clawsec` (Segurança, integridade de alma SOUL.md e drift detection)
*   `hermes-core-skills` (25 habilidades utilitárias de desenvolvimento e análise)
*   `hermes-skills` (Automação de SEO, WordPress e Marketing)
*   `academic-research-skills-hermes` (Escrita científica e pesquisas bibliográficas)
*   `hermes-skill-atlas` (Gerenciamento geográfico)

---

## 🚀 PASSO 4: Download de Modelos locais no Ollama (Host)

O Ollama roda diretamente no host do servidor para alta performance. Instale-o e puxe os modelos recomendados para a VPS de 8GB (modelos leves e extremamente rápidos):

```bash
# Instala o Ollama no host
curl -fsSL https://ollama.com/install.sh | sh

# Configura o Ollama para aceitar conexões vindas do container Docker
mkdir -p /etc/systemd/system/ollama.service.d
echo -e "[Service]\nEnvironment=\"OLLAMA_HOST=0.0.0.0\"" > /etc/systemd/system/ollama.service.d/host.conf
systemctl daemon-reload
systemctl restart ollama

# Baixa os modelos modernos otimizados para VPS de 8GB
ollama pull phi4:mini
ollama pull gemma4:e3b
```

---

## 🚀 PASSO 5: Inicialização e Pareamento do WhatsApp

### 5.1 Subir a Stack
Inicie os containers configurados pelo script:
```bash
docker compose up -d
```

### 5.2 Parear o WhatsApp
Como a API do WhatsApp roda de forma privada, faça um túnel local a partir da sua máquina física para o pareamento inicial:
```bash
# Execute este comando no terminal do seu COMPUTADOR LOCAL:
ssh -N -L 3000:127.0.0.1:3000 root@IP_DA_SUA_VPS
```

Agora, abra no seu navegador local:
➜ **`http://localhost:3000`**

*   Se selecionou **WAHA**: Clique em `sessions` -> Inicie a sessão `default` -> Escaneie o QR Code no seu celular.
*   Se selecionou **GOWA**: Escaneie o QR Code exibido diretamente no painel básico do GOWA.

---

## 🚀 PASSO 6: Acesso e Teste de Agentes e MCPs

Acesse as URLs seguras (HTTPS) para gerenciar seus agentes:

*   **Interface OpenClaw:** `https://open.vendoideias.com` (use o `OPENCLAW_GATEWAY_TOKEN` gerado no `.env` para logar).
*   **Hermes Dashboard:** `https://hermes.vendoideias.com` (chat interativo direto).

### 🛠️ Comandos de Diagnóstico e Controle
```bash
# Listar MCPs e Ferramentas ativos no OpenClaw
docker compose exec openclaw-vibestack openclaw mcp list

# Acompanhar logs em tempo real
docker compose logs -f openclaw-vibestack

# Reiniciar toda a infraestrutura
docker compose restart
```
