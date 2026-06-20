# agency/ — prompts dos agentes (templates)

Esta pasta traz os **prompts prontos** dos 6 agentes da agência de tráfego, para você carregar no **OpenClaw** (ou no **Hermes**). São **templates**: tudo que depende do seu contexto está marcado com **placeholders `{{ASSIM}}`** — troque-os antes de usar.

> ⚠️ **Este `README.md` é um guia, não um prompt.** Não cole o conteúdo dele em nenhum agente. Carregue só os arquivos `IDENTITY.md` / `SOUL.md` / `USER.md` / `TOOLS.md` / `AGENTS.md`.

## Como usar (resumo)

1. **Copie** a pasta `agency/` para o seu projeto (ou edite aqui mesmo no seu fork).
2. **Substitua todos os `{{...}}`** pelos seus valores (veja a tabela abaixo). Dica: um "localizar e substituir" no editor resolve rápido; confira que não sobrou nenhum `{{` com:
   ```
   grep -rn "{{" agency/
   ```
3. **Crie os agentes** no OpenClaw e cole cada arquivo no campo correspondente (veja "Os 5 arquivos por agente").
4. Para que um agente acione outro (ex.: Diretor → Analista), habilite **subagentes** no OpenClaw — veja o `README.md` da raiz, seção *"Passo 13 — Habilitar subagentes"*.
5. (Opcional) Para acordar agentes automaticamente em horários/intervalos, veja *"Pré-requisitos e crons (disparo automático)"* mais abaixo.

## Os 5 arquivos por agente

Cada pasta de agente tem até 5 arquivos (a CLI do OpenClaw os usa como blocos do prompt do agente; no Hermes, concatene-os no system prompt):

| Arquivo       | O que é                                                                 |
|---------------|-------------------------------------------------------------------------|
| `IDENTITY.md` | Nome, "vibe" e emoji do agente — quem ele é em uma linha.               |
| `SOUL.md`     | Personalidade e princípios — como ele pensa e fala.                    |
| `USER.md`     | Com quem ele fala (seus interlocutores) e em que tom/idioma.            |
| `TOOLS.md`    | Quais tools MCP ele pode usar e as regras de uso (só alguns agentes).  |
| `AGENTS.md`   | O papel operacional: fluxo, alçada, o que faz e o que **não** faz.     |

## Os 6 agentes e o fluxo

- **Diretor** 🎯 — porta única com você (o humano). Recebe tudo pelo seu canal, roteia e devolve. Não executa nada no Meta.
- **Analista** 📊 — só leitura de Meta Ads; entrega números + leitura, sem opinar.
- **Estrategista** ♟️ — decide a ação (ancorada em número). Tem alçada própria; acima dela, escala pro Diretor (= pede sua aprovação).
- **Copywriter** ✍️ — escreve as variações de texto do anúncio.
- **Criativo** 🎬 — produz a mídia (imagem/vídeo) via `media-editor` + `higgsfield`/`atlascloud`.
- **Gestor de Tráfego** 🛠️ — **único** que escreve no Meta Ads; executa só sob ordem da Estrategista (autônoma) ou do Diretor (aprovada por você).

```
Você → Diretor → Analista → Estrategista ─┬─ (na alçada) → Gestor → Meta Ads
                                          └─ (acima)     → Diretor → você aprova → Gestor
                              Estrategista → Copywriter / Criativo (quando há peça nova)
```

## Placeholders — troque todos

| Placeholder              | O que colocar                                                                 | Onde aparece |
|--------------------------|-------------------------------------------------------------------------------|--------------|
| `{{DONO}}`               | Nome do humano dono/decisor (quem aprova as ações).                           | Diretor, Estrategista, Gestor, Copywriter |
| `{{DONO_EMAIL}}`         | E-mail desse dono (identificação no sistema).                                 | Diretor |
| `{{CANAL}}`              | Canal por onde o dono fala com o Diretor — ex.: `WhatsApp` ou `Telegram`.     | Diretor |
| `{{PRODUTO_1}}`          | Nome do seu 1º produto/oferta.                                                | Copywriter |
| `{{TOM_PRODUTO_1}}`      | Tom de voz desse produto (ex.: "utilitário, direto, ganho concreto").         | Copywriter |
| `{{PRODUTO_2}}`          | Nome do seu 2º produto (apague se só tiver um).                               | Copywriter |
| `{{TOM_PRODUTO_2}}`      | Tom de voz do 2º produto.                                                     | Copywriter |
| `{{ALCADA_BUDGET_PCT}}`  | % de ajuste de budget que a Estrategista pode fazer **sem** te perguntar (ex.: `30`). | Estrategista |
| `{{ALCADA_GASTO_DIA}}`   | Teto de gasto incremental/dia que dispensa sua aprovação (ex.: `R$ 200/dia`). | Estrategista |
| `{{PESSOA_DA_MARCA}}`    | (Opcional) Quem é o rosto fixo dos criativos — dono, porta-voz, modelo.       | Criativo |
| `{{SLUG_DA_PESSOA}}`     | (Opcional) Apelido em minúsculas/sem espaço pra nomear a seed e o soul-id (ex.: `rosto-marca`). | Criativo |

> O `{{PESSOA_DA_MARCA}}` / `{{SLUG_DA_PESSOA}}` só importam se você for gerar criativos sempre com **um rosto fixo** (via soul-id do Higgsfield). Se não for, apague essa seção do `criativo/AGENTS.md`.

## Pré-requisitos e crons (disparo automático)

Os agentes acima podem ser **acordados automaticamente** pelo scheduler interno do OpenClaw (`openclaw cron`) — sem nenhum trigger externo. Cada job entrega uma mensagem ao agente, que processa um turno inteiro; com subagentes ligados, ele delega em cadeia (ex.: Diretor → Analista → Meta Ads).

> Todos os comandos rodam **dentro do container** `openclaw-vibestack`. Os jobs ficam em `/root/.openclaw/cron/jobs.json` e sobrevivem a restart / `docker compose down`.

### Pré-requisitos (uma vez)

**1. Habilitar subagentes** — necessário para qualquer cadeia entre agentes (ex.: Diretor delega ao Analista). Pule se for rodar cada agente isolado.

```bash
openclaw config set agents.defaults.subagents.maxSpawnDepth 2
openclaw config set agents.defaults.subagents.allowAgents '["*"]'
openclaw config set agents.defaults.subagents.announceTimeoutMs 300000
```

**2. Reiniciar o gateway e validar:**

```bash
openclaw gateway restart
openclaw config get agents.defaults.subagents
openclaw config get agents.list
```

A última saída deve listar `main` + os agentes da agência (`diretor`, `analista`, `estrategista`, `copywriter`, `criativo`, `gestor`). Se faltar algum, crie-o em `agents.list` — veja o *Passo 13* do `README.md` da raiz.

### Regras que evitam erro

- **`--session isolated`** é obrigatório para todo agente que **não** seja o default (`main`). Se você definiu o `diretor` como agente default, use `--session main` só para ele; para os demais, sempre `isolated`. (`--session main` num agente não-default falha com `sessionTarget "main" is only valid for the default agent`.)
- **`--keep-after-run`** mantém o job recorrente (fica `idle` entre execuções — normal). **`--delete-after-run`** roda só uma vez.
- **Nunca** instrua o agente a chamar `sessions_yield` — não existe neste build; quebra a cadeia (`Subagent announce give up`). O `sessions_spawn` já é bloqueante.
- **`--at`** aceita intervalo no formato número+unidade (`30s`, `5m`, `6h`). Para horário fixo do dia, confira `openclaw cron add --help` na sua versão.

### Um cron por agente

**Analista 📊 — leitura periódica (recorrente)** — só lê o Meta Ads e entrega números.

```bash
openclaw cron add \
  --name "Analista — leitura periódica" \
  --at "6h" \
  --tz "America/Sao_Paulo" \
  --session isolated \
  --agent analista \
  --keep-after-run \
  --message "Use meta-ads para ler as campanhas ativas. Entregue uma tabela Markdown (ID, Nome, Status, Spend, Impressions, Clicks, Conversions, CPA) + 3 bullets de leitura objetiva dos números. NÃO opine sobre ação."
```

**Diretor 🎯 — relatório diário (recorrente)** — orquestra: delega ao Analista e te entrega o resumo.

```bash
openclaw cron add \
  --name "Diretor — relatorio diario" \
  --at "24h" \
  --tz "America/Sao_Paulo" \
  --session isolated \
  --agent diretor \
  --keep-after-run \
  --message "Em UM turno bloqueante: chame sessions_spawn (runtime:'subagent', agentId:'analista', task:'leia as campanhas Meta Ads e devolva tabela + leitura'). Aguarde o tool-result. Depois sintetize um relatorio curto pro dono e envie pelo canal padrao."
```

> Se o `diretor` for seu agente default (`main`), troque `--session isolated` por `--session main`.

**Estrategista ♟️ — passada de decisão (recorrente)** — decide ações ancoradas nos números, dentro da alçada.

```bash
openclaw cron add \
  --name "Estrategista — decisao diaria" \
  --at "24h" \
  --tz "America/Sao_Paulo" \
  --session isolated \
  --agent estrategista \
  --keep-after-run \
  --message "Em UM turno: delegue ao analista a leitura atual via sessions_spawn. Com os numeros, decida acoes DENTRO da sua alcada. O que passar da alcada, escale pro diretor (NAO execute). NAO acione o gestor sem decisao fechada."
```

**Copywriter ✍️ / Criativo 🎬 — reativos (one-shot, sob briefing)**

> Estes dois são **reativos**: normalmente a Estrategista os **spawna** quando há peça nova (eles precisam de briefing). Agendá-los sozinhos raramente faz sentido. Se quiser mesmo um disparo agendado/manual, use o modelo abaixo com `--delete-after-run` e um `--message` com o briefing completo. Troque `--agent copywriter` por `--agent criativo` (e o texto) para a versão do Criativo.

```bash
openclaw cron add \
  --name "Copywriter — variacoes (one-shot)" \
  --at "30s" \
  --tz "America/Sao_Paulo" \
  --session isolated \
  --agent copywriter \
  --delete-after-run \
  --message "Escreva 3 variacoes de copy para {{PRODUTO_1}} (tom: {{TOM_PRODUTO_1}}). Cada uma com headline + corpo + CTA."
```

**Gestor 🛠️ — NÃO agende sozinho** ⚠️

> O Gestor é o **único** que escreve no Meta Ads e só age **sob ordem** da Estrategista (na alçada) ou do Diretor (aprovada por você). **Não** crie um cron autônomo apontando para o `gestor` — isso burlaria a aprovação e poderia gastar dinheiro sem revisão. Ele deve ser sempre **spawnado** por quem já tem a decisão fechada.

### Gerenciar os jobs

```bash
openclaw cron list
openclaw cron rm <jobId>
```

## Notas

- **Renomear agentes/papéis** é livre — mas se mudar um nome (ex.: "Gestor"), troque também as menções a ele nos outros arquivos.
- Os caminhos e nomes de tools (`/root/.openclaw/workspace/...`, `create_creative`, `finalize_for_meta`, etc.) **não** são placeholders: são reais do projeto, deixe como estão.
- Estes prompts são afinados para o conjunto de MCP servers do projeto (`meta-ads`, `media-editor`, `whatsapp`, `higgsfield`, `atlascloud`) — veja o `README.md` da raiz.
