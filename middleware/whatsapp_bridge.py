#!/usr/bin/env python3
"""Bridge inbound do WhatsApp: GOWA (webhook) -> agente Hermes -> resposta.

Fecha o ciclo do "canal" de WhatsApp (o envio já é coberto pelo MCP
whatsapp_gowa_mcp.py). Fluxo:

  WhatsApp -> GOWA (evento "message") --webhook POST--> ESTE bridge
           -> Hermes api_server (/v1/chat/completions, sessão por número)
           -> GOWA (/send/message) -> WhatsApp

Roda como processo no container openclaw-vibestack (subido pelo entrypoint),
escutando em 0.0.0.0:WA_BRIDGE_PORT — alcançável pelo gowa via DNS do
compose (http://openclaw-vibestack:<porta>/webhook). Só stdlib (http.server +
urllib), sem dependência nova.

Mídia inbound (imagem/áudio): além de texto, o bridge processa imagem e áudio.
Baixa os bytes da URL estática servida pelo GOWA, salva em _shared/assets/wa/ e
manda pro modelo (Hermes multimodal: image_url/input_audio; OpenClaw: via arquivo).
Se o modelo configurado não aceitar a modalidade, responde avisando que não
comporta.

Env:
  WA_BRIDGE_PORT            porta do listener (default 8765; só rede interna do compose)
  WA_BRIDGE_UPSTREAM        base do agente (default http://127.0.0.1:8642 = Hermes api_server)
  WA_BRIDGE_UPSTREAM_KEY    Bearer do api_server (= HERMES_API_SERVER_KEY)
  WA_BRIDGE_MODEL           modelo exposto (default 'hermes-agent')
  WA_BRIDGE_SESSION_PREFIX  prefixo da sessão por contato (default 'wa')
  WA_BRIDGE_ALLOWED_NUMBERS CSV de números permitidos (vazio = todos; recomendado preencher)
  WA_BRIDGE_UPSTREAM_TIMEOUT timeout (s) da chamada ao agente (default 0 = ILIMITADO; localhost)
  GOWA_BASE_URL             base do GOWA (default http://gowa:3000)
  GOWA_BASIC_AUTH           auth opcional do painel/API (formato user:password)
  GOWA_DEVICE_ID            ID do dispositivo/sessão (opcional, vira header X-Device-Id)
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Qual agente responde o canal: "hermes" (HTTP api_server) ou "openclaw" (CLI).
AGENT = os.environ.get("WA_BRIDGE_AGENT", "hermes").strip().lower()
PORT = int(os.environ.get("WA_BRIDGE_PORT", "8765"))
UPSTREAM = os.environ.get("WA_BRIDGE_UPSTREAM", "http://127.0.0.1:8642").rstrip("/")
UPSTREAM_KEY = os.environ.get("WA_BRIDGE_UPSTREAM_KEY", "")
MODEL = os.environ.get("WA_BRIDGE_MODEL", "hermes-agent")
SESSION_PREFIX = os.environ.get("WA_BRIDGE_SESSION_PREFIX", "wa")

UPSTREAM_TIMEOUT = int(os.environ.get("WA_BRIDGE_UPSTREAM_TIMEOUT", "0"))
_TIMEOUT = UPSTREAM_TIMEOUT if UPSTREAM_TIMEOUT > 0 else None
ACK_AFTER = int(os.environ.get("WA_BRIDGE_ACK_AFTER", "20"))  # avisa "processando" se passar disso (0 = off)

OPENCLAW_AGENT_ID = os.environ.get("WA_BRIDGE_OPENCLAW_AGENT", "").strip()

GOWA_BASE_URL = os.environ.get("GOWA_BASE_URL", "http://gowa:3000").rstrip("/")
GOWA_BASIC_AUTH = os.environ.get("GOWA_BASIC_AUTH", "")
GOWA_DEVICE_ID = os.environ.get("GOWA_DEVICE_ID", "")

# Allowlist de números
_allowed_raw = os.environ.get("WA_BRIDGE_ALLOWED_NUMBERS", "").strip()


def _br_variants(num: str) -> set:
    """Para celular BR, retorna {com_9, sem_9}."""
    n = re.sub(r"\D", "", num or "")
    out = {n} if n else set()
    if n.startswith("55"):
        ddd, rest = n[2:4], n[4:]
        if len(rest) == 9 and rest.startswith("9"):
            out.add("55" + ddd + rest[1:])      # tira o 9
        elif len(rest) == 8:
            out.add("55" + ddd + "9" + rest)     # poe o 9
    return out


ALLOWED: set = set()
for _n in _allowed_raw.split(","):
    if _n.strip():
        ALLOWED |= _br_variants(_n.strip())

RESET_CMDS = {"/reset", "/novo", "/new", "/clear", "/limpar", "/reiniciar"}

# Diretório persistente de mídia recebida
WA_MEDIA_DIR = "/root/.openclaw/workspace/_shared/assets/wa"

_MIME_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/webp": "webp", "image/gif": "gif",
    "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
    "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/amr": "amr",
    "video/mp4": "mp4", "video/3gpp": "3gp",
    "application/pdf": "pdf",
}


class ModelMediaUnsupported(Exception):
    """O modelo/endpoint do agente não aceita a mídia enviada."""


_seen_ids: dict[str, None] = {}
_seen_lock = threading.Lock()
_SEEN_MAX = 2000

_epoch: dict = {}
_epoch_lock = threading.Lock()


def _session_key(number: str) -> str:
    e = _epoch.get(number, 0)
    base = f"{SESSION_PREFIX}:{number}"
    return base if e == 0 else f"{base}:{e}"


def _reset_session(number: str) -> None:
    with _epoch_lock:
        _epoch[number] = _epoch.get(number, 0) + 1


def _log(msg: str) -> None:
    print(f"[wa-bridge] {msg}", flush=True)


def _seen(msg_id: str) -> bool:
    if not msg_id:
        return False
    with _seen_lock:
        if msg_id in _seen_ids:
            return True
        _seen_ids[msg_id] = None
        if len(_seen_ids) > _SEEN_MAX:
            for k in list(_seen_ids)[: _SEEN_MAX // 2]:
                _seen_ids.pop(k, None)
    return False


def _digits(jid: str) -> str:
    """Extrai número de JID '5511...@s.whatsapp.net'."""
    head = re.split(r"[:@]", str(jid or ""), 1)[0]
    return re.sub(r"\D", "", head)


def _headers() -> dict[str, str]:
    hdrs = {}
    if GOWA_BASIC_AUTH:
        auth_bytes = GOWA_BASIC_AUTH.encode("utf-8")
        auth_b64 = base64.b64encode(auth_bytes).decode("utf-8")
        hdrs["Authorization"] = f"Basic {auth_b64}"
    if GOWA_DEVICE_ID:
        hdrs["X-Device-Id"] = GOWA_DEVICE_ID
    return hdrs


def _extract(data: dict) -> dict | None:
    """Parseia o payload de mensagem do webhook do GOWA."""
    from_me = data.get("is_from_me", data.get("isFromMe", False))
    if from_me:
        return None

    chat_id = str(data.get("chat_id") or "")
    if "@g.us" in chat_id or "@broadcast" in chat_id or "status@" in chat_id:
        return None

    sender = data.get("from") or chat_id
    number = _digits(sender)
    if not number:
        return None

    msg_id = str(data.get("id") or "")
    text = data.get("body") or ""

    media = None
    media_kinds = ["image", "audio", "video", "document"]
    for kind in media_kinds:
        media_obj = data.get(kind)
        if media_obj:
            if isinstance(media_obj, dict):
                path = media_obj.get("path") or ""
                caption = media_obj.get("caption") or ""
            else:
                path = str(media_obj)
                caption = ""
            
            if path:
                ext = path.split(".")[-1].lower() if "." in path else ""
                mime = "application/octet-stream"
                for m_name, m_ext in _MIME_EXT.items():
                    if m_ext == ext:
                        mime = m_name
                        break
                
                # O arquivo é servido estaticamente pelo GOWA
                media_url = f"{GOWA_BASE_URL}/{path.lstrip('/')}"
                media = {
                    "kind": kind,
                    "caption": str(caption or ""),
                    "mimetype": mime,
                    "media_url": media_url,
                    "base64": None,
                    "message": data,
                    "msg_id": msg_id,
                }
                text = str(caption or text)
                break

    return {"number": number, "text": str(text), "msg_id": msg_id, "media": media}


def _ask_hermes(number: str, text: str) -> str:
    if not UPSTREAM_KEY:
        return "(bridge sem WA_BRIDGE_UPSTREAM_KEY configurada)"
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{UPSTREAM}/v1/chat/completions",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {UPSTREAM_KEY}",
            "X-Hermes-Session-Id": _session_key(number),
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    try:
        return out["choices"][0]["message"]["content"] or "(resposta vazia)"
    except (KeyError, IndexError, TypeError):
        return "(não consegui interpretar a resposta do Hermes)"


def _ask_openclaw(number: str, text: str) -> str:
    cmd = ["openclaw", "agent", "--message", text, "--session-key", _session_key(number), "--json"]
    if OPENCLAW_AGENT_ID:
        cmd += ["--agent", OPENCLAW_AGENT_ID]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    if r.returncode != 0:
        return f"(openclaw agent falhou: {(r.stderr or r.stdout).strip()[:200]})"
    out_raw = r.stdout.strip()
    try:
        out = json.loads(out_raw)
    except json.JSONDecodeError:
        return out_raw or "(resposta vazia do openclaw)"
    
    if isinstance(out, dict):
        res = out.get("result")
        if isinstance(res, dict):
            payloads = res.get("payloads")
            if isinstance(payloads, list):
                parts = [p["text"] for p in payloads
                         if isinstance(p, dict) and isinstance(p.get("text"), str) and p["text"].strip()]
                if parts:
                    return "\n\n".join(parts)
            meta = res.get("meta")
            if isinstance(meta, dict):
                for mk in ("finalAssistantVisibleText", "finalAssistantRawText"):
                    mv = meta.get(mk)
                    if isinstance(mv, str) and mv.strip():
                        return mv
    for k in ("reply", "text", "message", "content", "response", "output", "finalText"):
        v = out.get(k) if isinstance(out, dict) else None
        if isinstance(v, str) and v.strip():
            return v
    return out_raw or "(não consegui interpretar a resposta do openclaw)"


def _ask_agent(number: str, text: str) -> str:
    if AGENT == "openclaw":
        return _ask_openclaw(number, text)
    return _ask_hermes(number, text)


def _send_whatsapp(number: str, text: str) -> None:
    """Envia a resposta de volta pelo GOWA (/send/message)."""
    phone = f"{number}@s.whatsapp.net"
    body = json.dumps({"phone": phone, "message": text}).encode("utf-8")
    
    headers = _headers()
    headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(
        f"{GOWA_BASE_URL}/send/message",
        data=body,
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        resp.read()


def _reply_safe(number: str, text: str) -> None:
    try:
        _send_whatsapp(number, text)
    except Exception as e:
        _log(f"erro enviando aviso pra {number}: {e}")


def _process(number: str, text: str) -> None:
    _log(f"in  <- {number}: {text[:80]!r}")
    done = threading.Event()

    def _ack_if_slow() -> None:
        if ACK_AFTER > 0 and not done.wait(ACK_AFTER):
            _reply_safe(number, "🛠️ Tô processando — tarefas maiores levam alguns minutos. Já te respondo.")

    threading.Thread(target=_ack_if_slow, daemon=True).start()
    try:
        reply = _ask_agent(number, text)
        done.set()
        _send_whatsapp(number, reply)
        _log(f"out -> {number}: {reply[:80]!r}")
    except urllib.error.HTTPError as e:
        done.set()
        _log(f"ERRO HTTP {e.code} processando {number}: {e.read()[:200]!r}")
        _reply_safe(number, "⚠️ Deu um erro ao processar. Pode tentar de novo?")
    except Exception as e:
        done.set()
        _log(f"ERRO processando {number}: {e}")
        msg = ("⏳ A operação demorou mais que o limite e foi interrompida. "
               "Tente dividir em passos menores, ou repita o pedido.") if "timed out" in str(e).lower() \
              else "⚠️ Não consegui concluir agora. Pode tentar de novo?"
        _reply_safe(number, msg)


# --- Mídia inbound ---------------------------------------------------------

def _http_get_bytes(url: str) -> bytes:
    headers = _headers()
    headers["User-Agent"] = "wa-bridge"
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def _save_media(number: str, media: dict, data_bytes: bytes, mime: str) -> str:
    ext = _MIME_EXT.get((mime or "").split(";")[0].strip().lower(), "bin")
    stamp = re.sub(r"\W", "", media.get("msg_id") or "") or str(int(time.time()))
    try:
        os.makedirs(WA_MEDIA_DIR, exist_ok=True)
    except OSError:
        pass
    path = f"{WA_MEDIA_DIR}/{number}-{stamp}.{ext}"
    with open(path, "wb") as f:
        f.write(data_bytes)
    return path


def _audio_format(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    return {
        "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp3": "mp3",
        "audio/mp4": "m4a", "audio/aac": "aac",
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/amr": "amr",
    }.get(m, "ogg")


def _ask_hermes_media(number: str, kind: str, data_bytes: bytes, mime: str, caption: str) -> str:
    if not UPSTREAM_KEY:
        return "(bridge sem WA_BRIDGE_UPSTREAM_KEY configurada)"
    b64 = base64.b64encode(data_bytes).decode("ascii")
    prompt = caption.strip() or (
        "Descreva e responda sobre esta imagem." if kind == "image"
        else "Transcreva e responda a este áudio."
    )
    if kind == "image":
        part = {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/jpeg'};base64,{b64}"}}
    else:
        part = {"type": "input_audio", "input_audio": {"data": b64, "format": _audio_format(mime)}}
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}, part]}],
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{UPSTREAM}/v1/chat/completions", data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {UPSTREAM_KEY}",
            "X-Hermes-Session-Id": _session_key(number),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        if 400 <= e.code < 500:
            raise ModelMediaUnsupported(f"HTTP {e.code}: {detail}")
        raise
    if isinstance(out, dict) and out.get("error"):
        raise ModelMediaUnsupported(str(out.get("error")))
    try:
        content = out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        content = None
    if not content:
        raise ModelMediaUnsupported("resposta vazia do modelo")
    return content


def _ask_openclaw_media(number: str, kind: str, caption: str, path: str) -> str:
    desc = "imagem" if kind == "image" else "áudio"
    msg = f"[O usuário enviou um(a) {desc} pelo WhatsApp, salvo em {path}."
    if caption:
        msg += f" Legenda: {caption}."
    msg += " Interprete com suas ferramentas se possível; senão, peça os detalhes por texto.]"
    return _ask_openclaw(number, msg)


def _process_media(number: str, media: dict) -> None:
    kind = media["kind"]
    caption = media.get("caption") or ""
    _log(f"in  <- {number}: [{kind}] caption={caption[:60]!r}")
    done = threading.Event()

    def _ack_if_slow() -> None:
        if ACK_AFTER > 0 and not done.wait(ACK_AFTER):
            _reply_safe(number, "🛠️ Recebi sua mídia, tô processando — já te respondo.")

    threading.Thread(target=_ack_if_slow, daemon=True).start()

    if kind not in ("image", "audio"):
        done.set()
        _reply_safe(number, "Recebi seu arquivo, mas por ora só interpreto *imagem* e *áudio*. Pode mandar por texto?")
        return

    try:
        mime = media["mimetype"]
        data_bytes = _http_get_bytes(media["media_url"])
        path = _save_media(number, media, data_bytes, mime)
        _log(f"     media salva: {path} ({len(data_bytes)} bytes, {mime or '?'})")
        if AGENT == "openclaw":
            reply = _ask_openclaw_media(number, kind, caption, path)
        else:
            reply = _ask_hermes_media(number, kind, data_bytes, mime, caption)
        done.set()
        _send_whatsapp(number, reply)
        _log(f"out -> {number}: {reply[:80]!r}")
    except ModelMediaUnsupported as e:
        done.set()
        _log(f"modelo nao comporta {kind} p/ {number}: {e}")
        tipo = "imagens" if kind == "image" else "áudios"
        _reply_safe(number, f"⚠️ O modelo configurado neste agente não interpreta {tipo}. Por favor, descreva por texto.")
    except Exception as e:
        done.set()
        _log(f"ERRO processando mídia de {number}: {e}")
        _reply_safe(number, "⚠️ Não consegui baixar/processar a mídia. Pode tentar de novo ou enviar por texto?")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _ok(self, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):
        self._ok()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        self._ok()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return
        
        # O GOWA envia o webhook com event == "message"
        event = str(payload.get("event") or "").lower()
        if event != "message":
            return
            
        data = payload.get("payload") or {}
        extracted = _extract(data)
        if not extracted:
            return
            
        number = extracted["number"]
        text = extracted["text"]
        msg_id = extracted["msg_id"]
        media = extracted["media"]
        if _seen(msg_id):
            return
            
        if ALLOWED and not (_br_variants(number) & ALLOWED):
            _log(f"ignorado (fora da allowlist): {number}")
            return
            
        if media is not None:
            threading.Thread(target=_process_media, args=(number, media), daemon=True).start()
            return
            
        if text.strip().lower() in RESET_CMDS:
            _reset_session(number)
            _log(f"reset solicitado por {number} -> sessao {_session_key(number)}")
            threading.Thread(
                target=_reply_safe,
                args=(number, "🔄 Conversa reiniciada. Pode mandar a próxima mensagem."),
                daemon=True,
            ).start()
            return
        threading.Thread(target=_process, args=(number, text), daemon=True).start()


def _provision() -> None:
    """Tenta reconectar o dispositivo no boot se o GOWA estiver pronto."""
    if not GOWA_BASE_URL:
        return
    for attempt in range(1, 31):
        try:
            dev_id = GOWA_DEVICE_ID or "default"
            headers = _headers()
            req = urllib.request.Request(f"{GOWA_BASE_URL}/app/reconnect?device_id={dev_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            _log(f"dispositivo '{dev_id}' do GOWA reconectado com sucesso.")
            return
        except Exception as e:
            _log(f"aguardando GOWA... ({e}) (tentativa {attempt})")
        time.sleep(10)


def main() -> None:
    threading.Thread(target=_provision, daemon=True).start()
    dest = f"openclaw CLI (agent={OPENCLAW_AGENT_ID or 'default'})" if AGENT == "openclaw" else f"hermes {UPSTREAM}"
    _log(f"escutando em 0.0.0.0:{PORT} (webhook POST /webhook) -> {dest} -> resposta via {GOWA_BASE_URL}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
