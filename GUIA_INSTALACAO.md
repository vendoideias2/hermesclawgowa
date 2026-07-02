# 🗺️ GUIA DEFINITIVO — Instalação da Stack HermesClawGowa com WAHA, Traefik e HTTPS

Este é o roteiro de instalação definitivo para configurar a sua VPS com Swap de 12GB, compiladores nativos, e rodar a stack **HermesClawGowa** utilizando o **WAHA** (WhatsApp HTTP API) em substituição ao GOWA. Toda a comunicação externa dos painéis web será protegida com HTTPS via **Traefik** nos domínios **open.vendoideias.com** e **hermes.vendoideias.com**.

---

## 📋 VISÃO GERAL — O Que Cada Fase Faz

| Fase | O que faz | Onde roda |
| :--- | :--- | :--- |
| **1** | Aponta registros DNS dos subdomínios para o IP da VPS | Provedor de Domínio |
| **2** | Executa o instalador interativo (Swap, Ferramentas, Docker, Modelos e Configs) | VPS |
| **3** | Inicia a stack com Docker Compose | VPS |
| **4** | Pareia o WhatsApp no console web do WAHA | Navegador local |
| **5** | Gerenciamento e testes do sistema | VPS / Navegador |

---

## 🚀 FASE 1: Apontamento de DNS

No painel de gerenciamento do seu domínio (`vendoideias.com`), crie dois registros do **Tipo A** apontando para o IP público do seu servidor VPS:

*   `open.vendoideias.com` ➜ `IP_DA_SUA_VPS`
*   `hermes.vendoideias.com` ➜ `IP_DA_SUA_VPS`

---

## 🚀 FASE 2: Execução do Instalador na VPS

Conecte-se na sua VPS via SSH:
```bash
ssh root@IP_DA_SUA_VPS
```

Clone o repositório, dê permissão de execução e rode o novo instalador automático:
```bash
cd /root
git clone https://github.com/vendoideias2/hermesclawgowa.git
cd hermesclawgowa
chmod +x install.sh
./install.sh
```

### ⚙️ O que o `install.sh` faz de forma nativa e integrada:
*   ✅ **Ferramentas do Host:** Pergunta se deseja instalar os compiladores e interpretadores mais recentes do **Node.js LTS**, **Python 3 (+ venv/pip)** e **Go Compiler** diretamente no host da VPS.
*   ✅ **Swap de 12GB:** Cria e configura de forma fixa um arquivo de swapfile de 12GB e otimiza a swappiness do kernel Linux (essencial para rodar LLMs em 8GB de RAM).
*   ✅ **Docker e Compose:** Garante a instalação do Docker Engine e do plugin docker-compose-plugin v2.
*   ✅ **Firewall UFW:** Configura o firewall nativo do Linux abrindo apenas as portas essenciais do projeto (`22`, `80`, `443`, `3000`, `18789`, `9119`, `8642`).
*   ✅ **Configuração Interativa do `.env`:** Solicita dados como domínio, chaves de API (Meta Ads, Backblaze B2, WAHA API Key) e gera chaves de segurança randômicas robustas automaticamente.
*   ✅ **Modelos locais do Ollama:** Inicia o download local no host dos modelos modernos (`phi4-mini` de 3.8B e `gemma4:e2b` de 2B).
*   ✅ **Skills Automáticas:** Cria o script de gerenciamento e instala automaticamente as principais skills do catálogo (`hermes-skill-atlas`, `clawsec`, `hermes-core-skills`, `hermes-skills` (de marketing/SEO) e `academic-research-skills-hermes`) na pasta de dados.

---

## 🚀 FASE 3: Inicialização da Stack

Após o build das imagens Docker do OpenClaw/Hermes terminar no script de instalação, suba os serviços em segundo plano:

```bash
docker compose up -d
```

Verifique se todos os serviços estão saudáveis:
```bash
docker compose ps
```

### 📊 Serviços Rodando:
*   `hermesclawgowa-traefik-1` ➜ Proxy reverso HTTPS nas portas `80` e `443`.
*   `hermesclawgowa-waha-1` ➜ WhatsApp HTTP API na porta `3000`.
*   `hermesclawgowa-openclaw-vibestack-1` ➜ Ambiente integrado do OpenClaw (API + UI) e Hermes Agent.

---

## 🚀 FASE 4: Pareamento do WhatsApp via WAHA

O serviço do WAHA roda internamente por motivos de segurança. Para acessá-lo e parear o seu celular, faça um túnel SSH do seu computador local:

No terminal da sua máquina física (computador local):
```bash
ssh -N -L 3000:127.0.0.1:3000 root@IP_DA_SUA_VPS
```

No seu navegador local, abra:
➜ **`http://localhost:3000`**

1.  Acesse o dashboard do **WAHA**.
2.  Inicie a sessão `default` (caso ainda não esteja iniciada).
3.  Visualize o **QR Code** gerado no painel e escaneie com o aplicativo do WhatsApp do seu celular.
4.  O status mudará para `WORKING` quando o pareamento for concluído.

---

## 🚀 FASE 5: Acesso aos Serviços e Testes

Agora, você já pode acessar as suas interfaces públicas criptografadas com certificado SSL emitido automaticamente pelo Traefik:

| Painel / Interface | Endereço Web | Descrição |
| :--- | :--- | :--- |
| **OpenClaw UI** | `https://open.vendoideias.com` | Painel web interativo de IA |
| **Hermes Dashboard** | `https://hermes.vendoideias.com` | Chat e gestão do Hermes Agent |

### 5.1 Verificar as Skills e MCPs Ativos
Entre no container do OpenClaw e liste os MCPs integrados:
```bash
docker compose exec openclaw-vibestack openclaw mcp list
```
*Saída esperada:* `meta-ads`, `media-editor`, `whatsapp`, `higgsfield`, `atlascloud`.

### 5.2 Exemplos de Prompt para Testar
Envie mensagens pelo WhatsApp conectado ou pelas interfaces web:
*   *"Envie uma mensagem de WhatsApp para 5517999999999 dizendo Olá!"* (Chama o MCP do WhatsApp)
*   *"/reset"* (Reinicia a sessão de conversação e memória local do agente)

---

## 🔧 COMANDOS ÚTEIS DE GERENCIAMENTO

```bash
# Ver logs do Traefik (emissão de certificados SSL)
docker compose logs -f traefik

# Ver logs do bridge do WhatsApp e do agente
docker compose logs -f openclaw-vibestack

# Reiniciar toda a infraestrutura
docker compose restart

# Parar os containers
docker compose down

# Ver credenciais geradas automaticamente (.env.secrets)
cat .env.secrets
```
