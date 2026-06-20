#!/usr/bin/env python3
"""MCP server stdio que expõe o envio de WhatsApp via GOWA (Go WhatsApp Web Multi-Device).

GOWA (https://github.com/aldinokemal/go-whatsapp-web-multidevice) roda como
serviço no docker-compose (porta 3000). Este middleware é o cliente REST consumido
pelos agentes (OpenClaw e Hermes) — envio de mensagens, mídias e gestão de pareamento.

Env:
  GOWA_BASE_URL     base da API (default http://gowa:3000, DNS do compose)
  GOWA_BASIC_AUTH   auth opcional do painel/API (formato user:password)
  GOWA_DEVICE_ID    ID do dispositivo/sessão (opcional, vira header X-Device-Id)
"""
import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("whatsapp-gowa")

BASE_URL = os.environ.get("GOWA_BASE_URL", "http://gowa:3000").rstrip("/")
BASIC_AUTH = os.environ.get("GOWA_BASIC_AUTH", "")
DEVICE_ID = os.environ.get("GOWA_DEVICE_ID", "")


def _normalize_phone(phone: str) -> str:
    """Garante que o número termine com @s.whatsapp.net se não for grupo."""
    phone = phone.strip()
    if not phone:
        return ""
    if "@" in phone:
        return phone
    return f"{phone}@s.whatsapp.net"


def _headers() -> dict[str, str]:
    """Retorna os headers padrão, injetando Basic Auth e Device ID se disponíveis."""
    hdrs = {}
    if BASIC_AUTH:
        auth_bytes = BASIC_AUTH.encode("utf-8")
        auth_b64 = base64.b64encode(auth_bytes).decode("utf-8")
        hdrs["Authorization"] = f"Basic {auth_b64}"
    if DEVICE_ID:
        hdrs["X-Device-Id"] = DEVICE_ID
    return hdrs


def _req(method: str, path: str, body: dict | None = None, files: dict | None = None) -> Any:
    """Chama a API do GOWA. Suporta JSON e Multipart."""
    url = f"{BASE_URL}{path}"
    headers = _headers()

    if files:
        # Requisição Multipart/Form-Data
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex}"
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        
        parts = []
        if body:
            for name, val in body.items():
                parts.append(f"--{boundary}".encode("utf-8"))
                parts.append(f'Content-Disposition: form-data; name="{name}"'.encode("utf-8"))
                parts.append(b"")
                parts.append(str(val).encode("utf-8"))
        
        for field_name, (filename, content, mimetype) in files.items():
            parts.append(f"--{boundary}".encode("utf-8"))
            parts.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode("utf-8"))
            parts.append(f"Content-Type: {mimetype}".encode("utf-8"))
            parts.append(b"")
            parts.append(content)
            
        parts.append(f"--{boundary}--".encode("utf-8"))
        parts.append(b"")
        data = b"\r\n".join(parts)
    else:
        # Requisição JSON
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
    """Inicia a sessão de WhatsApp no GOWA. GOWA gerencia dispositivos dinamicamente.
    
    Equivalente a registrar/iniciar o login de um novo dispositivo.
    """
    dev_id = name or DEVICE_ID or "default"
    # Chamamos o status ou login para garantir a inicialização
    path = f"/app/login?device_id={dev_id}"
    return _req("GET", path)


@mcp.tool()
def wa_connect() -> Any:
    """Conecta ou reconecta a sessão do dispositivo GOWA."""
    dev_id = DEVICE_ID or "default"
    return _req("GET", f"/app/reconnect?device_id={dev_id}")


@mcp.tool()
def wa_get_qr() -> Any:
    """Retorna o QR code em Base64 para parear o WhatsApp no celular."""
    dev_id = DEVICE_ID or "default"
    return _req("GET", f"/app/login?device_id={dev_id}")


@mcp.tool()
def wa_instance_status() -> Any:
    """Retorna o status da conexão (connected / disconnected)."""
    dev_id = DEVICE_ID or "default"
    res = _req("GET", f"/app/status?device_id={dev_id}")
    if isinstance(res, dict) and "results" in res:
        st = res["results"].get("instance_status") or res["results"].get("status")
        return {"status": st, "device_id": dev_id}
    # Fallback se não logado
    if isinstance(res, dict) and res.get("status") is False:
        return {"status": "disconnected", "device_id": dev_id}
    return res


# ============================================================
# Envio
# ============================================================

@mcp.tool()
def wa_send_text(number: str, text: str) -> Any:
    """Envia mensagem de texto.

    number: número com código do país, só dígitos ou JID (ex: '5511999999999').
    text: conteúdo da mensagem.
    """
    phone = _normalize_phone(number)
    body = {"phone": phone, "message": text}
    return _req("POST", "/send/message", body=body)


@mcp.tool()
def wa_send_link(number: str, text: str) -> Any:
    """Envia texto contendo link. No GOWA o preview do link é automático."""
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
    caption: legenda opcional (para imagem/vídeo).
    filename: nome do arquivo (recomendado para document).
    """
    phone = _normalize_phone(number)
    is_url = media.startswith("http://") or media.startswith("https://")
    
    # 1. Se for URL e imagem, o GOWA aceita envio simples via JSON
    if is_url and mediatype == "image":
        body = {
            "phone": phone,
            "image_url": media,
            "caption": caption or "",
            "compress": True
        }
        return _req("POST", "/send/image", body=body)

    # 2. Caso contrário, baixamos os bytes da URL ou decodificamos o base64
    if is_url:
        try:
            req = urllib.request.Request(media, headers={"User-Agent": "wa-bridge"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data_bytes = r.read()
            # Deriva o filename se não fornecido
            fname = filename or media.split("/")[-1].split("?")[0] or "file"
        except Exception as e:
            return {"error": f"Falha ao baixar mídia da URL: {e}"}
    else:
        # Trata base64 (removendo data-url prefix se presente)
        if "," in media:
            media = media.split(",", 1)[1]
        try:
            data_bytes = base64.b64decode(media)
            fname = filename or f"file-{uuid.uuid4().hex[:8]}"
        except Exception as e:
            return {"error": f"Falha ao decodificar base64: {e}"}

    # Define o mimetype e endpoint com base no mediatype
    mimetype = "application/octet-stream"
    if mediatype == "image":
        mimetype = "image/jpeg"
        endpoint = "/send/image"
        fname = fname if "." in fname else f"{fname}.jpg"
        body = {"phone": phone, "caption": caption or "", "compress": "true"}
        files = {"image": (fname, data_bytes, mimetype)}
    elif mediatype == "audio":
        mimetype = "audio/mp3"
        endpoint = "/send/audio"
        fname = fname if "." in fname else f"{fname}.mp3"
        body = {"phone": phone}
        files = {"audio": (fname, data_bytes, mimetype)}
    elif mediatype == "video":
        mimetype = "video/mp4"
        endpoint = "/send/video"
        fname = fname if "." in fname else f"{fname}.mp4"
        body = {"phone": phone, "caption": caption or ""}
        files = {"video": (fname, data_bytes, mimetype)}
    else:  # document
        mimetype = "application/pdf" if fname.endswith(".pdf") else "application/octet-stream"
        endpoint = "/send/document"
        body = {"phone": phone}
        files = {"document": (fname, data_bytes, mimetype)}

    return _req("POST", endpoint, body=body, files=files)


if __name__ == "__main__":
    mcp.run()
