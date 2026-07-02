#!/usr/bin/env python3
"""MCP server stdio que expõe o envio de WhatsApp via WAHA (WhatsApp HTTP API).

WAHA (https://waha.devlike.pro/) roda como serviço no docker-compose (porta 3000).
Este middleware é o cliente REST consumido pelos agentes (OpenClaw e Hermes).

Env:
  GOWA_BASE_URL     base da API (default http://waha:3000)
  WAHA_API_KEY      token/chave de API do WAHA (opcional)
  GOWA_DEVICE_ID    ID do dispositivo/sessão (default 'default')
"""
import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whatsapp-mcp")

BASE_URL = os.environ.get("WAHA_BASE_URL", "http://waha:3000").rstrip("/")
API_KEY = os.environ.get("WAHA_API_KEY", "")
DEVICE_ID = os.environ.get("WAHA_DEVICE_ID", "default")


def _normalize_phone(phone: str) -> str:
    """Garante que o número termine com @c.us se não for grupo."""
    phone = phone.strip()
    if not phone:
        return ""
    if "@" in phone:
        return phone
    return f"{phone}@c.us"


def _headers() -> dict[str, str]:
    """Retorna os headers padrão do WAHA."""
    hdrs = {}
    if API_KEY:
        hdrs["X-Api-Key"] = API_KEY
    return hdrs


def _req(method: str, path: str, body: dict | None = None) -> Any:
    """Chama a API do WAHA via JSON."""
    url = f"{BASE_URL}{path}"
    headers = _headers()
    
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8"))
        except Exception:
            err_body = {"raw": str(e)}
        return {"error": err_body, "status": e.code, "method": method, "path": path}
    except Exception as e:
        return {"error": str(e), "method": method, "path": path}


# ============================================================
# Instância / pareamento
# ============================================================

@mcp.tool()
def wa_create_instance(name: str | None = None, token: str | None = None, webhook: str | None = None) -> Any:
    """Inicia/Registra a sessão de WhatsApp no WAHA."""
    dev_id = name or DEVICE_ID or "default"
    body = {
        "name": dev_id,
        "config": {
            "webhooks": [
                {
                    "url": webhook or "http://openclaw-vibestack:8765/webhook",
                    "events": ["message"]
                }
            ]
        }
    }
    return _req("POST", "/api/sessions", body=body)


@mcp.tool()
def wa_connect() -> Any:
    """Conecta ou reconecta a sessão do dispositivo WAHA."""
    dev_id = DEVICE_ID or "default"
    return _req("POST", f"/api/sessions/{dev_id}/start")


@mcp.tool()
def wa_get_qr() -> Any:
    """Retorna as instruções de pareamento do WhatsApp via QR Code."""
    return {
        "message": "Acesse a interface web do WAHA na porta 3000 para escanear o QR Code de pareamento.",
        "url": "http://localhost:3000"
    }


@mcp.tool()
def wa_instance_status() -> Any:
    """Retorna o status da conexão (connected / disconnected)."""
    dev_id = DEVICE_ID or "default"
    res = _req("GET", f"/api/sessions/{dev_id}")
    if isinstance(res, dict) and "status" in res:
        status = res["status"]
        st = "connected" if status == "WORKING" else "disconnected"
        return {"status": st, "device_id": dev_id, "waha_status": status}
    return {"status": "disconnected", "device_id": dev_id}


# ============================================================
# Envio
# ============================================================

@mcp.tool()
def wa_send_text(number: str, text: str) -> Any:
    """Envia mensagem de texto.

    number: número com código do país, só dígitos (ex: '5511999999999').
    text: conteúdo da mensagem.
    """
    phone = _normalize_phone(number)
    body = {
        "chatId": phone,
        "text": text,
        "session": DEVICE_ID or "default"
    }
    return _req("POST", "/api/sendText", body=body)


@mcp.tool()
def wa_send_link(number: str, text: str) -> Any:
    """Envia texto contendo link. O preview é gerado automaticamente pelo WAHA."""
    return wa_send_text(number, text)


@mcp.tool()
def wa_send_media(
    number: str,
    media: str,
    mediatype: str = "image",
    caption: str | None = None,
    filename: str | None = None,
) -> Any:
    """Envia mídia (imagem/vídeo/áudio/documento).

    media: URL pública OU string base64 da mídia.
    mediatype: image | video | audio | document.
    caption: legenda opcional.
    filename: nome do arquivo (opcional).
    """
    phone = _normalize_phone(number)
    is_url = media.startswith("http://") or media.startswith("https://")
    
    file_obj = {}
    if is_url:
        file_obj["url"] = media
        file_obj["filename"] = filename or media.split("/")[-1].split("?")[0] or "file"
    else:
        # Caso base64
        if "," in media:
            media = media.split(",", 1)[1]
        file_obj["data"] = media
        file_obj["filename"] = filename or f"file-{uuid.uuid4().hex[:8]}"

    body = {
        "chatId": phone,
        "file": file_obj,
        "caption": caption or "",
        "session": DEVICE_ID or "default"
    }
    return _req("POST", "/api/sendFile", body=body)


if __name__ == "__main__":
    mcp.run()
