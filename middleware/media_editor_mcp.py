#!/usr/bin/env python3
"""MCP server stdio que expõe ffmpeg + Backblaze B2 como tools tipados.

Voltado ao agente Criativo (agency/criativo/AGENTS.md): seeds e derivações
vivem todas no B2 (S3-compatible), a interface entre tools são chaves puras
(sem `b2://`), e o pipeline termina em `finalize_for_meta` que materializa
o arquivo em /root/.openclaw/workspace/_shared/creatives/ para o Gestor
publicar via meta_ads_cli_mcp.create_creative.

Auth: lê B2_KEY_ID, B2_APP_KEY, B2_BUCKET, B2_ENDPOINT_URL do env.

Prefixos B2 permitidos: inbox/, seeds/, work/, final/, requests/, meta/.

Pattern espelha middleware/meta_ads_cli_mcp.py: retorno sempre dict, sem
exception bubble. Em falha de ffmpeg, dict contém 'error', 'cmd' e
'duration_ms'.
"""
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("media-editor")

BUCKET = os.environ.get("B2_BUCKET", "")
ENDPOINT = os.environ.get("B2_ENDPOINT_URL", "")
KEY_ID = os.environ.get("B2_KEY_ID", "")
APP_KEY = os.environ.get("B2_APP_KEY", "")

ALLOWED_PREFIXES = ("inbox/", "seeds/", "work/", "final/", "requests/", "meta/")

FINAL_LOCAL_DIR = Path("/root/.openclaw/workspace/_shared/creatives")

# Specs resumidas de Meta Ads — usado por probe(validate_for=...)
META_SPECS: dict[str, dict[str, Any]] = {
    "meta_image_feed": {
        "min_width": 1080,
        "min_height": 1080,
        "aspect_min": 1.0,
        "aspect_max": 1.91,
        "max_size_bytes": 30 * 1024 * 1024,
    },
    "meta_image_story": {
        "min_width": 1080,
        "min_height": 1920,
        "aspect": 9 / 16,
        "tolerance": 0.02,
        "max_size_bytes": 30 * 1024 * 1024,
    },
    "meta_video_feed": {
        "min_duration": 1,
        "max_duration": 241,
        "aspect_min": 0.5625,
        "aspect_max": 1.0,
        "max_size_bytes": 4 * 1024**3,
        "video_codec": "h264",
        "audio_codec": "aac",
        "max_fps": 60,
    },
    "meta_video_reels": {
        "min_duration": 0,
        "max_duration": 90,
        "aspect": 9 / 16,
        "tolerance": 0.02,
        "min_width": 500,
        "max_size_bytes": 4 * 1024**3,
        "video_codec": "h264",
        "audio_codec": "aac",
    },
}


# ============================================================
# Helpers privados
# ============================================================

_B2 = None


def _b2() -> Any:
    """Client S3-compatible para Backblaze B2. Cacheado em modulo."""
    global _B2
    if _B2 is None:
        _B2 = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id=KEY_ID,
            aws_secret_access_key=APP_KEY,
        )
    return _B2


def _check_b2_config() -> dict[str, str] | None:
    if not (BUCKET and ENDPOINT and KEY_ID and APP_KEY):
        return {
            "error": "Backblaze B2 nao configurado",
            "hint": "Defina B2_BUCKET, B2_ENDPOINT_URL, B2_KEY_ID, B2_APP_KEY no .env",
        }
    return None


def _validate_key(key: str) -> dict[str, str] | None:
    """Rejeita chaves invalidas. Retorna None se OK, dict de erro se ruim."""
    if not isinstance(key, str) or not key:
        return {"error": "chave vazia"}
    if key.startswith("/"):
        return {"error": f"chave nao pode comecar com '/': {key}"}
    if ".." in key:
        return {"error": f"chave nao pode conter '..': {key}"}
    if not any(key.startswith(p) for p in ALLOWED_PREFIXES):
        return {
            "error": f"prefixo nao permitido para '{key}'",
            "hint": f"use um de: {', '.join(ALLOWED_PREFIXES)}",
        }
    return None


def _ext_of(key: str) -> str:
    tail = key.rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[1].lower() if "." in tail else ""


def _content_type_for(ext: str) -> str:
    return mimetypes.types_map.get("." + ext.lower(), "application/octet-stream")


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9-]+", "-", s.strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _b2_head(key: str) -> dict[str, Any] | None:
    try:
        return _b2().head_object(Bucket=BUCKET, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        return None


def _b2_download(key: str, local_path: Path) -> dict[str, str] | None:
    try:
        _b2().download_file(BUCKET, key, str(local_path))
        return None
    except (ClientError, BotoCoreError) as e:
        return {"error": f"falha ao baixar '{key}': {e}"}


def _b2_upload(local_path: Path, key: str, content_type: str | None = None) -> dict[str, str] | None:
    extra: dict[str, Any] = {}
    if content_type:
        extra["ContentType"] = content_type
    try:
        _b2().upload_file(str(local_path), BUCKET, key, ExtraArgs=extra)
        return None
    except (ClientError, BotoCoreError) as e:
        return {"error": f"falha ao subir '{key}': {e}"}


def _ffmpeg(*args: str, timeout: int = 600) -> dict[str, Any]:
    """Executa ffmpeg com flags seguras para o stdio MCP.

    -nostdin: nao le stdin (que e o canal MCP). Sem isso ffmpeg em alguns
              modos come bytes do canal e corrompe o protocolo.
    -loglevel error: descarta progresso/info; evita inflar RSS em logs grandes.
    -y: sobrescreve output sem perguntar.
    stdin=DEVNULL: belt-and-suspenders.
    """
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *args]
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"ffmpeg timeout apos {timeout}s", "cmd": " ".join(cmd)}
    duration_ms = int((time.monotonic() - t0) * 1000)
    if r.returncode != 0:
        return {
            "error": r.stderr.strip() or f"ffmpeg exit {r.returncode}",
            "cmd": " ".join(cmd),
            "duration_ms": duration_ms,
        }
    return {"ok": True, "cmd": " ".join(cmd), "duration_ms": duration_ms}


def _ffprobe(local_path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(local_path),
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False,
            stdin=subprocess.DEVNULL, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"error": "ffprobe timeout 60s"}
    if r.returncode != 0:
        return {"error": r.stderr.strip() or f"ffprobe exit {r.returncode}"}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {"error": f"ffprobe JSON invalido: {e}"}


def _derive_key(input_key: str, op: str, params: dict[str, Any], ext: str) -> str:
    """Gera output_key determinístico em work/<slug>/<stem>__<op>-<hash8>.<ext>."""
    parts = input_key.split("/")
    tail = parts[-1]
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail

    if input_key.startswith("work/") and len(parts) >= 3:
        slug = parts[1]
    else:
        slug = stem

    payload = json.dumps({"input": input_key, "op": op, "params": params}, sort_keys=True, default=str)
    h = hashlib.sha256(payload.encode()).hexdigest()[:8]
    return f"work/{slug}/{stem}__{op}-{h}.{ext}"


def _sidecar_key(output_key: str) -> str:
    return f"meta/{output_key}.json"


def _read_sidecar(output_key: str) -> dict[str, Any] | None:
    try:
        obj = _b2().get_object(Bucket=BUCKET, Key=_sidecar_key(output_key))
        return json.loads(obj["Body"].read())
    except (ClientError, BotoCoreError, json.JSONDecodeError):
        return None


def _write_sidecar(
    output_key: str,
    op: str,
    input_keys: list[str],
    params: dict[str, Any],
    ffmpeg_cmd: str,
    duration_ms: int,
) -> None:
    payload = {
        "op": op,
        "input_keys": input_keys,
        "params": params,
        "ffmpeg_cmd": ffmpeg_cmd,
        "duration_ms": duration_ms,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _b2().put_object(
            Bucket=BUCKET,
            Key=_sidecar_key(output_key),
            Body=json.dumps(payload, indent=2, default=str).encode(),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError):
        pass  # sidecar e best-effort, nao quebra a operacao


def _aspect_filter(width: int, height: int, mode: str) -> str:
    """Monta -vf para image/video_fit conforme mode."""
    if mode == "cover":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    if mode == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if mode == "crop":
        return f"crop={width}:{height}"
    return f"scale={width}:{height}"  # stretch


def _pos_xy(position: str, margin: int = 40) -> tuple[str, str] | None:
    """Retorna (x, y) em expressoes ffmpeg para overlay/drawtext."""
    pos_map = {
        "center": ("(W-w)/2", "(H-h)/2"),
        "top": ("(W-w)/2", str(margin)),
        "bottom": ("(W-w)/2", f"H-h-{margin}"),
        "top-left": (str(margin), str(margin)),
        "top-right": (f"W-w-{margin}", str(margin)),
        "bottom-left": (str(margin), f"H-h-{margin}"),
        "bottom-right": (f"W-w-{margin}", f"H-h-{margin}"),
    }
    return pos_map.get(position)


def _escape_drawtext(text: str) -> str:
    """Escapa caracteres especiais para o filtro drawtext do ffmpeg."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def _transform(
    op: str,
    input_keys: list[str],
    params: dict[str, Any],
    output_key: str | None,
    overwrite: bool,
    output_ext: str,
    build_args: Callable[[list[Path], Path], list[str]],
) -> dict[str, Any]:
    """Pipeline: valida -> baixa -> ffmpeg -> sobe -> sidecar."""
    for k in input_keys:
        err = _validate_key(k)
        if err:
            return err
    cfg_err = _check_b2_config()
    if cfg_err:
        return cfg_err

    if output_key is None:
        output_key = _derive_key(input_keys[0], op, params, ext=output_ext)
    err = _validate_key(output_key)
    if err:
        return err
    if not (output_key.startswith("work/") or output_key.startswith("final/")):
        return {
            "error": f"output_key '{output_key}' deve estar em work/ ou final/",
        }

    head = _b2_head(output_key)
    if head and not overwrite:
        return {
            "output_key": output_key,
            "was_cached": True,
            "size_bytes": head["ContentLength"],
            "sidecar": _read_sidecar(output_key),
        }

    with tempfile.TemporaryDirectory(prefix="media-") as tmp:
        tmp_p = Path(tmp)
        local_inputs: list[Path] = []
        for i, k in enumerate(input_keys):
            ext = _ext_of(k) or "bin"
            local = tmp_p / f"in{i}.{ext}"
            d_err = _b2_download(k, local)
            if d_err:
                return d_err
            local_inputs.append(local)

        out_local = tmp_p / f"out.{output_ext}"
        ff_args = build_args(local_inputs, out_local)
        res = _ffmpeg(*ff_args)
        if "error" in res:
            return {**res, "output_key": output_key}

        u_err = _b2_upload(out_local, output_key, _content_type_for(output_ext))
        if u_err:
            return u_err
        size = out_local.stat().st_size

        _write_sidecar(output_key, op, input_keys, params, res["cmd"], res["duration_ms"])

        return {
            "output_key": output_key,
            "was_cached": False,
            "size_bytes": size,
            "ffmpeg_ms": res["duration_ms"],
        }


def _list_objects(prefix: str, max_keys: int) -> dict[str, Any]:
    """Implementacao crua de listagem — separada da tool pra reuso interno."""
    if not prefix:
        return {
            "error": "prefix obrigatorio",
            "hint": "use 'inbox/', 'seeds/', 'work/<slug>/', 'final/', 'requests/'",
        }
    if ".." in prefix or prefix.startswith("/"):
        return {"error": "prefix invalido"}
    err = _check_b2_config()
    if err:
        return err
    max_keys = max(1, min(max_keys, 1000))
    try:
        r = _b2().list_objects_v2(Bucket=BUCKET, Prefix=prefix, MaxKeys=max_keys)
    except (ClientError, BotoCoreError) as e:
        return {"error": str(e)}
    items = [
        {
            "key": o["Key"],
            "size_bytes": o["Size"],
            "last_modified": o["LastModified"].isoformat(),
        }
        for o in r.get("Contents", [])
    ]
    return {
        "prefix": prefix,
        "count": len(items),
        "truncated": r.get("IsTruncated", False),
        "items": items,
    }


# ============================================================
# Storage
# ============================================================

@mcp.tool()
def b2_list(prefix: str, max_keys: int = 100) -> Any:
    """Lista objetos no bucket B2 sob um prefixo.

    Args:
      prefix: obrigatorio. Use 'inbox/', 'seeds/video/', 'work/<slug>/', etc.
      max_keys: limite (1..1000, default 100). Mantenha baixo pra nao inundar contexto.
    """
    return _list_objects(prefix, max_keys)


@mcp.tool()
def b2_upload_local(local_path: str, key: str) -> Any:
    """Sobe um arquivo local para o B2 sob a chave dada.

    Uso raro — só para bootstrap manual de seeds quando humano deixou um
    arquivo no container ao inves do app B2.
    """
    err = _validate_key(key)
    if err:
        return err
    err = _check_b2_config()
    if err:
        return err
    p = Path(local_path)
    if not p.is_file():
        return {"error": f"arquivo local nao existe: {local_path}"}
    u_err = _b2_upload(p, key, _content_type_for(_ext_of(key)))
    if u_err:
        return u_err
    return {"key": key, "size_bytes": p.stat().st_size}


@mcp.tool()
def b2_get_info(key: str) -> Any:
    """Retorna metadados (tamanho, content-type, last_modified) e sidecar."""
    err = _validate_key(key)
    if err:
        return err
    err = _check_b2_config()
    if err:
        return err
    head = _b2_head(key)
    if not head:
        return {"error": "nao encontrado", "key": key}
    return {
        "key": key,
        "size_bytes": head["ContentLength"],
        "content_type": head.get("ContentType"),
        "last_modified": head["LastModified"].isoformat(),
        "sidecar": _read_sidecar(key),
    }


@mcp.tool()
def b2_delete(key: str) -> Any:
    """Deleta um objeto. Tambem deleta o sidecar correspondente se existir."""
    err = _validate_key(key)
    if err:
        return err
    err = _check_b2_config()
    if err:
        return err
    try:
        _b2().delete_object(Bucket=BUCKET, Key=key)
        _b2().delete_object(Bucket=BUCKET, Key=_sidecar_key(key))
    except (ClientError, BotoCoreError) as e:
        return {"error": str(e)}
    return {"deleted": key}


# ============================================================
# Inbox / seeds
# ============================================================

@mcp.tool()
def request_human_media(slug: str, instructions: str, deadline_iso: str) -> Any:
    """Registra um pedido de gravacao/foto pelo humano em requests/<slug>.json.

    O humano consulta esses pedidos e sobe o arquivo em inbox/ pelo app B2.
    Depois o agente roda list_inbox + claim_inbox_item para classificar.

    Args:
      slug: identificador (ex: 'depoimento-cliente-01').
      instructions: o que o humano deve gravar/fotografar.
      deadline_iso: prazo no formato ISO 8601 (ex: '2026-06-01T18:00:00Z').
    """
    err = _check_b2_config()
    if err:
        return err
    s = _slugify(slug)
    if not s:
        return {"error": "slug invalido apos slugify"}
    key = f"requests/{s}.json"
    payload = {
        "slug": s,
        "instructions": instructions,
        "deadline_iso": deadline_iso,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    try:
        _b2().put_object(
            Bucket=BUCKET,
            Key=key,
            Body=json.dumps(payload, indent=2).encode(),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError) as e:
        return {"error": str(e)}
    return {"key": key, "request": payload}


@mcp.tool()
def list_inbox(prefix: str = "") -> Any:
    """Lista uploads humanos pendentes em inbox/.

    Args:
      prefix: sub-prefixo opcional (ex: 'depoimento-' para filtrar).
    """
    return _list_objects(f"inbox/{prefix or ''}", max_keys=200)


@mcp.tool()
def claim_inbox_item(inbox_key: str, seed_kind: str, seed_slug: str) -> Any:
    """Move inbox/<X> para seeds/<kind>/<slug>.<ext>, classificando o asset.

    Args:
      inbox_key: chave atual em inbox/ (ex: 'inbox/IMG_1234.jpg').
      seed_kind: 'image' | 'video' | 'audio'.
      seed_slug: identificador semantico (ex: 'depoimento-ana').
    """
    if not inbox_key.startswith("inbox/"):
        return {"error": "inbox_key deve comecar com 'inbox/'"}
    if seed_kind not in ("image", "video", "audio"):
        return {"error": "seed_kind deve ser 'image', 'video' ou 'audio'"}
    s = _slugify(seed_slug)
    if not s:
        return {"error": "seed_slug invalido"}
    err = _check_b2_config()
    if err:
        return err
    ext = _ext_of(inbox_key)
    if not ext:
        return {"error": "inbox_key sem extensao"}
    dest = f"seeds/{seed_kind}/{s}.{ext}"
    try:
        client = _b2()
        client.copy_object(
            Bucket=BUCKET,
            Key=dest,
            CopySource={"Bucket": BUCKET, "Key": inbox_key},
        )
        client.delete_object(Bucket=BUCKET, Key=inbox_key)
    except (ClientError, BotoCoreError) as e:
        return {"error": str(e)}
    return {"moved_from": inbox_key, "moved_to": dest}


@mcp.tool()
def list_seeds(kind: str | None = None) -> Any:
    """Lista midias-base ja classificadas em seeds/.

    Args:
      kind: filtra por 'image', 'video' ou 'audio'. None lista tudo.
    """
    if kind and kind not in ("image", "video", "audio"):
        return {"error": "kind deve ser 'image', 'video' ou 'audio'"}
    prefix = f"seeds/{kind}/" if kind else "seeds/"
    return _list_objects(prefix, max_keys=500)


# ============================================================
# Image
# ============================================================

@mcp.tool()
def image_fit(
    input_key: str,
    width: int,
    height: int,
    mode: str = "cover",
    output_format: str | None = None,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Redimensiona/recorta imagem para WxH.

    Args:
      mode:
        - 'cover': mantem proporcao, recorta excesso (padrao p/ criativos).
        - 'contain': mantem proporcao, padding preto nas bordas.
        - 'crop': corta centralizado em WxH sem escalar.
        - 'stretch': estica forcado (deforma).
      output_format: 'jpg' | 'png' | 'webp'. Default: mantem extensao do input.
      output_key: chave de saida. Default: derivada de hash(input + params).
      overwrite: sobrescreve se ja existir (default False).
    """
    if mode not in ("cover", "contain", "crop", "stretch"):
        return {"error": "mode invalido", "hint": "cover|contain|crop|stretch"}
    if width <= 0 or height <= 0:
        return {"error": "width e height devem ser > 0"}
    in_ext = _ext_of(input_key) or "jpg"
    out_ext = (output_format or in_ext).lower()
    if out_ext == "jpeg":
        out_ext = "jpg"
    if out_ext not in ("jpg", "png", "webp"):
        return {"error": "output_format deve ser jpg, png ou webp"}
    params = {"width": width, "height": height, "mode": mode, "output_format": out_ext}

    def build(ins: list[Path], out: Path) -> list[str]:
        return ["-i", str(ins[0]), "-vf", _aspect_filter(width, height, mode), str(out)]

    return _transform("image_fit", [input_key], params, output_key, overwrite, out_ext, build)


@mcp.tool()
def image_overlay(
    input_key: str,
    kind: str,
    position: str = "center",
    overlay_key: str | None = None,
    text: str | None = None,
    font_size: int = 48,
    font_color: str = "white",
    box: bool = True,
    box_color: str = "black@0.5",
    scale_pct: int = 100,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Sobrepoe texto ou imagem sobre uma imagem.

    Args:
      kind: 'text' ou 'image'.
      position: 'center', 'top', 'bottom', 'top-left', 'top-right',
                'bottom-left', 'bottom-right'.
      overlay_key: chave da imagem overlay (obrigatorio se kind='image').
      text: texto (obrigatorio se kind='text').
      font_size: px (texto).
      font_color: ex 'white', '#FF0000'.
      box: desenha caixa atras do texto.
      box_color: ex 'black@0.5'.
      scale_pct: escala do overlay-imagem em % (100 = original).
    """
    if kind not in ("text", "image"):
        return {"error": "kind deve ser 'text' ou 'image'"}
    if kind == "text" and not text:
        return {"error": "text obrigatorio quando kind='text'"}
    if kind == "image" and not overlay_key:
        return {"error": "overlay_key obrigatorio quando kind='image'"}
    xy = _pos_xy(position)
    if xy is None:
        return {"error": "position invalida",
                "hint": "center|top|bottom|top-left|top-right|bottom-left|bottom-right"}
    x_part, y_part = xy
    out_ext = _ext_of(input_key) or "jpg"

    if kind == "text":
        params = {
            "kind": "text", "text": text, "position": position,
            "font_size": font_size, "font_color": font_color,
            "box": box, "box_color": box_color,
        }
        safe = _escape_drawtext(text or "")
        box_part = f":box=1:boxcolor={box_color}:boxborderw=10" if box else ""
        vf = (
            f"drawtext=text='{safe}':fontsize={font_size}"
            f":fontcolor={font_color}{box_part}:x={x_part}:y={y_part}"
        )

        def build(ins: list[Path], out: Path) -> list[str]:
            return ["-i", str(ins[0]), "-vf", vf, str(out)]

        return _transform(
            "image_overlay_text", [input_key], params, output_key, overwrite, out_ext, build,
        )

    err = _validate_key(overlay_key or "")
    if err:
        return err
    params = {
        "kind": "image", "overlay_key": overlay_key,
        "position": position, "scale_pct": scale_pct,
    }

    def build_img(ins: list[Path], out: Path) -> list[str]:
        if scale_pct != 100:
            fc = (
                f"[1:v]scale=iw*{scale_pct / 100}:ih*{scale_pct / 100}[ov];"
                f"[0:v][ov]overlay={x_part}:{y_part}"
            )
        else:
            fc = f"[0:v][1:v]overlay={x_part}:{y_part}"
        return ["-i", str(ins[0]), "-i", str(ins[1]), "-filter_complex", fc, str(out)]

    return _transform(
        "image_overlay_image",
        [input_key, overlay_key or ""],
        params, output_key, overwrite, out_ext, build_img,
    )


# ============================================================
# Video
# ============================================================

@mcp.tool()
def video_trim(
    input_key: str,
    start_seconds: float,
    end_seconds: float,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Corta video entre start_seconds e end_seconds. Re-encoda para precisao."""
    if start_seconds < 0 or end_seconds <= start_seconds:
        return {"error": "start_seconds >= 0 e end_seconds > start_seconds"}
    out_ext = _ext_of(input_key) or "mp4"
    params = {"start": start_seconds, "end": end_seconds}

    def build(ins: list[Path], out: Path) -> list[str]:
        return [
            "-ss", str(start_seconds),
            "-to", str(end_seconds),
            "-i", str(ins[0]),
            "-c:v", "libx264", "-c:a", "aac",
            str(out),
        ]

    return _transform("video_trim", [input_key], params, output_key, overwrite, out_ext, build)


@mcp.tool()
def video_fit(
    input_key: str,
    width: int,
    height: int,
    mode: str = "cover",
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Redimensiona/recorta video para WxH. Mesma semantica de image_fit."""
    if mode not in ("cover", "contain", "crop", "stretch"):
        return {"error": "mode invalido", "hint": "cover|contain|crop|stretch"}
    if width <= 0 or height <= 0:
        return {"error": "width e height devem ser > 0"}
    out_ext = _ext_of(input_key) or "mp4"
    params = {"width": width, "height": height, "mode": mode}

    def build(ins: list[Path], out: Path) -> list[str]:
        return [
            "-i", str(ins[0]),
            "-vf", _aspect_filter(width, height, mode),
            "-c:v", "libx264", "-c:a", "copy",
            str(out),
        ]

    return _transform("video_fit", [input_key], params, output_key, overwrite, out_ext, build)


@mcp.tool()
def video_concat(
    input_keys: list[str],
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Concatena videos na ordem dada. Reencoda para garantir compatibilidade."""
    if not input_keys or len(input_keys) < 2:
        return {"error": "passe ao menos 2 input_keys"}
    out_ext = _ext_of(input_keys[0]) or "mp4"
    params = {"inputs": input_keys}

    def build(ins: list[Path], out: Path) -> list[str]:
        args: list[str] = []
        for p in ins:
            args += ["-i", str(p)]
        n = len(ins)
        parts = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
        fc = f"{parts}concat=n={n}:v=1:a=1[outv][outa]"
        args += [
            "-filter_complex", fc,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-c:a", "aac",
            str(out),
        ]
        return args

    return _transform("video_concat", input_keys, params, output_key, overwrite, out_ext, build)


@mcp.tool()
def video_overlay(
    input_key: str,
    kind: str,
    position: str = "bottom",
    overlay_key: str | None = None,
    text: str | None = None,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    font_size: int = 48,
    font_color: str = "white",
    box: bool = True,
    box_color: str = "black@0.5",
    scale_pct: int = 100,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Sobrepoe caption (texto) ou logo (imagem) sobre video, com timing opcional.

    Args:
      kind: 'text' ou 'image'.
      position: center|top|bottom|top-left|top-right|bottom-left|bottom-right.
      start_seconds/end_seconds: janela em que o overlay aparece.
                                 end_seconds=None = ate o fim do video.
      Demais args iguais ao image_overlay.
    """
    if kind not in ("text", "image"):
        return {"error": "kind deve ser 'text' ou 'image'"}
    if kind == "text" and not text:
        return {"error": "text obrigatorio quando kind='text'"}
    if kind == "image" and not overlay_key:
        return {"error": "overlay_key obrigatorio quando kind='image'"}
    xy = _pos_xy(position, margin=80)
    if xy is None:
        return {"error": "position invalida"}
    x_part, y_part = xy
    out_ext = _ext_of(input_key) or "mp4"

    enable = ""
    if start_seconds > 0 or end_seconds is not None:
        end_val = end_seconds if end_seconds is not None else 99999.0
        enable = f":enable='between(t,{start_seconds},{end_val})'"

    if kind == "text":
        params = {
            "kind": "text", "text": text, "position": position,
            "font_size": font_size, "start": start_seconds, "end": end_seconds,
        }
        safe = _escape_drawtext(text or "")
        box_part = f":box=1:boxcolor={box_color}:boxborderw=10" if box else ""
        vf = (
            f"drawtext=text='{safe}':fontsize={font_size}:fontcolor={font_color}"
            f"{box_part}:x={x_part}:y={y_part}{enable}"
        )

        def build_t(ins: list[Path], out: Path) -> list[str]:
            return ["-i", str(ins[0]), "-vf", vf, "-c:a", "copy", str(out)]

        return _transform(
            "video_overlay_text", [input_key], params, output_key, overwrite, out_ext, build_t,
        )

    err = _validate_key(overlay_key or "")
    if err:
        return err
    params = {
        "kind": "image", "overlay_key": overlay_key,
        "position": position, "scale_pct": scale_pct,
        "start": start_seconds, "end": end_seconds,
    }

    def build_img(ins: list[Path], out: Path) -> list[str]:
        if scale_pct != 100:
            fc = (
                f"[1:v]scale=iw*{scale_pct / 100}:ih*{scale_pct / 100}[ov];"
                f"[0:v][ov]overlay={x_part}:{y_part}{enable}"
            )
        else:
            fc = f"[0:v][1:v]overlay={x_part}:{y_part}{enable}"
        return [
            "-i", str(ins[0]), "-i", str(ins[1]),
            "-filter_complex", fc,
            "-c:a", "copy",
            str(out),
        ]

    return _transform(
        "video_overlay_image",
        [input_key, overlay_key or ""],
        params, output_key, overwrite, out_ext, build_img,
    )


@mcp.tool()
def video_audio(
    input_key: str,
    mode: str,
    audio_key: str | None = None,
    mix_db: float = 0.0,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Manipula trilha de audio do video.

    Args:
      mode:
        - 'add': mixa nova faixa com a original. mix_db ajusta ganho da nova.
        - 'replace': substitui completamente pelo audio_key.
        - 'strip': remove audio.
        - 'extract': extrai audio para .mp3.
    """
    if mode not in ("add", "replace", "strip", "extract"):
        return {"error": "mode invalido", "hint": "add|replace|strip|extract"}

    if mode == "extract":
        out_ext = "mp3"
        params = {"mode": "extract"}

        def build_ext(ins: list[Path], out: Path) -> list[str]:
            return ["-i", str(ins[0]), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(out)]

        return _transform(
            "video_audio_extract", [input_key], params, output_key, overwrite, out_ext, build_ext,
        )

    if mode == "strip":
        out_ext = _ext_of(input_key) or "mp4"
        params = {"mode": "strip"}

        def build_strip(ins: list[Path], out: Path) -> list[str]:
            return ["-i", str(ins[0]), "-c:v", "copy", "-an", str(out)]

        return _transform(
            "video_audio_strip", [input_key], params, output_key, overwrite, out_ext, build_strip,
        )

    if not audio_key:
        return {"error": f"audio_key obrigatorio para mode='{mode}'"}
    err = _validate_key(audio_key)
    if err:
        return err
    out_ext = _ext_of(input_key) or "mp4"

    if mode == "replace":
        params = {"mode": "replace", "audio_key": audio_key}

        def build_rep(ins: list[Path], out: Path) -> list[str]:
            return [
                "-i", str(ins[0]), "-i", str(ins[1]),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "aac", "-shortest",
                str(out),
            ]

        return _transform(
            "video_audio_replace", [input_key, audio_key], params, output_key, overwrite, out_ext, build_rep,
        )

    params = {"mode": "add", "audio_key": audio_key, "mix_db": mix_db}

    def build_add(ins: list[Path], out: Path) -> list[str]:
        fc = (
            f"[1:a]volume={mix_db}dB[a1];"
            f"[0:a][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        )
        return [
            "-i", str(ins[0]), "-i", str(ins[1]),
            "-filter_complex", fc,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac",
            str(out),
        ]

    return _transform(
        "video_audio_add", [input_key, audio_key], params, output_key, overwrite, out_ext, build_add,
    )


@mcp.tool()
def video_extract_frame(
    input_key: str,
    time_seconds: float,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Extrai um frame do video em time_seconds como JPG.

    Use para gerar thumbnails ou seeds de imagem a partir de video gravado.
    """
    if time_seconds < 0:
        return {"error": "time_seconds >= 0"}
    out_ext = "jpg"
    params = {"time": time_seconds}

    def build(ins: list[Path], out: Path) -> list[str]:
        return [
            "-ss", str(time_seconds),
            "-i", str(ins[0]),
            "-frames:v", "1", "-q:v", "2",
            str(out),
        ]

    return _transform("video_extract_frame", [input_key], params, output_key, overwrite, out_ext, build)


@mcp.tool()
def video_transcode(
    input_key: str,
    codec: str = "h264",
    bitrate_kbps: int = 4000,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Reencoda video mudando codec/bitrate sem alterar dimensoes.

    Args:
      codec: 'h264' (recomendado para Meta) ou 'vp9'.
      bitrate_kbps: bitrate de video em kbps.
    """
    if codec not in ("h264", "vp9"):
        return {"error": "codec deve ser 'h264' ou 'vp9'"}
    if bitrate_kbps <= 0:
        return {"error": "bitrate_kbps > 0"}
    out_ext = "mp4" if codec == "h264" else "webm"
    vcodec = "libx264" if codec == "h264" else "libvpx-vp9"
    params = {"codec": codec, "bitrate_kbps": bitrate_kbps}

    def build(ins: list[Path], out: Path) -> list[str]:
        return [
            "-i", str(ins[0]),
            "-c:v", vcodec, "-b:v", f"{bitrate_kbps}k",
            "-c:a", "aac",
            str(out),
        ]

    return _transform("video_transcode", [input_key], params, output_key, overwrite, out_ext, build)


@mcp.tool()
def video_loop(
    input_key: str,
    mode: str,
    target_seconds: float,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Estende video por repeticao ou boomerang ate target_seconds.

    Args:
      mode:
        - 'repeat': repete em loop ate atingir target_seconds.
        - 'boomerang': vai-e-volta (concat com versao reversa). Sem audio.
    """
    if mode not in ("repeat", "boomerang"):
        return {"error": "mode deve ser 'repeat' ou 'boomerang'"}
    if target_seconds <= 0:
        return {"error": "target_seconds > 0"}
    out_ext = _ext_of(input_key) or "mp4"
    params = {"mode": mode, "target_seconds": target_seconds}

    if mode == "repeat":
        def build_rep(ins: list[Path], out: Path) -> list[str]:
            return [
                "-stream_loop", "-1", "-i", str(ins[0]),
                "-t", str(target_seconds),
                "-c:v", "libx264", "-c:a", "aac",
                str(out),
            ]

        return _transform(
            "video_loop_repeat", [input_key], params, output_key, overwrite, out_ext, build_rep,
        )

    def build_boom(ins: list[Path], out: Path) -> list[str]:
        fc = (
            "[0:v]split[v1][v2];[v2]reverse[v2r];"
            "[v1][v2r]concat=n=2:v=1:a=0[vout]"
        )
        return [
            "-stream_loop", "-1", "-i", str(ins[0]),
            "-filter_complex", fc,
            "-map", "[vout]",
            "-t", str(target_seconds),
            "-c:v", "libx264", "-an",
            str(out),
        ]

    return _transform(
        "video_loop_boomerang", [input_key], params, output_key, overwrite, out_ext, build_boom,
    )


@mcp.tool()
def video_speed(
    input_key: str,
    factor: float,
    output_key: str | None = None,
    overwrite: bool = False,
) -> Any:
    """Acelera ou desacelera video.

    factor=2.0 dobra velocidade, 0.5 reduz a metade. Audio ajustado via atempo
    (encadeado quando fora do range valido 0.5..100).
    """
    if factor <= 0:
        return {"error": "factor > 0"}
    out_ext = _ext_of(input_key) or "mp4"
    params = {"factor": factor}

    remaining = factor
    chain: list[str] = []
    while remaining > 100.0:
        chain.append("atempo=100")
        remaining /= 100.0
    while remaining < 0.5:
        chain.append("atempo=0.5")
        remaining /= 0.5
    chain.append(f"atempo={remaining}")
    a_filter = ",".join(chain)
    v_filter = f"setpts={1 / factor}*PTS"

    def build(ins: list[Path], out: Path) -> list[str]:
        fc = f"[0:v]{v_filter}[v];[0:a]{a_filter}[a]"
        return [
            "-i", str(ins[0]),
            "-filter_complex", fc,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-c:a", "aac",
            str(out),
        ]

    return _transform("video_speed", [input_key], params, output_key, overwrite, out_ext, build)


# ============================================================
# Probe + validate
# ============================================================

def _probe_internal(key: str) -> dict[str, Any]:
    """ffprobe puro, sem validacao. Util pro finalize_for_meta."""
    err = _validate_key(key)
    if err:
        return err
    err = _check_b2_config()
    if err:
        return err
    with tempfile.TemporaryDirectory(prefix="probe-") as tmp:
        local = Path(tmp) / f"in.{_ext_of(key) or 'bin'}"
        d_err = _b2_download(key, local)
        if d_err:
            return d_err
        meta = _ffprobe(local)
        if "error" in meta:
            return meta
        size_bytes = local.stat().st_size

    fmt = meta.get("format", {})
    streams = meta.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fps: float | None = None
    if v and v.get("r_frame_rate"):
        try:
            num, den = v["r_frame_rate"].split("/")
            den_i = int(den)
            fps = round(int(num) / den_i, 2) if den_i else None
        except (ValueError, ZeroDivisionError):
            fps = None

    return {
        "key": key,
        "width": v.get("width") if v else None,
        "height": v.get("height") if v else None,
        "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
        "size_bytes": size_bytes,
        "video_codec": v.get("codec_name") if v else None,
        "audio_codec": a.get("codec_name") if a else None,
        "fps": fps,
        "format_name": fmt.get("format_name"),
    }


def _validate_against_spec(result: dict[str, Any], validate_for: str) -> dict[str, Any]:
    if validate_for not in META_SPECS:
        return {
            **result,
            "valid": False,
            "violations": [f"validate_for desconhecido: {validate_for}"],
            "validated_for": validate_for,
        }
    spec = META_SPECS[validate_for]
    width = result.get("width")
    height = result.get("height")
    duration = result.get("duration_seconds")
    size_bytes = result.get("size_bytes")
    v_codec = result.get("video_codec")
    a_codec = result.get("audio_codec")
    fps = result.get("fps")

    violations: list[str] = []
    if "min_width" in spec and width and width < spec["min_width"]:
        violations.append(f"width {width} < min {spec['min_width']}")
    if "min_height" in spec and height and height < spec["min_height"]:
        violations.append(f"height {height} < min {spec['min_height']}")
    if "min_duration" in spec and duration is not None and duration < spec["min_duration"]:
        violations.append(f"duration {duration:.2f} < min {spec['min_duration']}")
    if "max_duration" in spec and duration is not None and duration > spec["max_duration"]:
        violations.append(f"duration {duration:.2f} > max {spec['max_duration']}")
    if "max_size_bytes" in spec and size_bytes and size_bytes > spec["max_size_bytes"]:
        violations.append(f"size {size_bytes} > max {spec['max_size_bytes']}")
    if "aspect" in spec and width and height:
        actual = width / height
        tol = spec.get("tolerance", 0.02)
        if abs(actual - spec["aspect"]) > tol:
            violations.append(f"aspect {actual:.4f} != {spec['aspect']:.4f} (+/-{tol})")
    if "aspect_min" in spec and width and height:
        actual = width / height
        if actual < spec["aspect_min"] or actual > spec.get("aspect_max", 99.0):
            violations.append(
                f"aspect {actual:.4f} fora de [{spec['aspect_min']}, {spec.get('aspect_max', 'inf')}]"
            )
    if "video_codec" in spec and v_codec and v_codec != spec["video_codec"]:
        violations.append(f"video_codec {v_codec} != {spec['video_codec']}")
    if "audio_codec" in spec and a_codec and a_codec != spec["audio_codec"]:
        violations.append(f"audio_codec {a_codec} != {spec['audio_codec']}")
    if "max_fps" in spec and fps and fps > spec["max_fps"]:
        violations.append(f"fps {fps} > max {spec['max_fps']}")

    return {
        **result,
        "valid": not violations,
        "violations": violations,
        "validated_for": validate_for,
    }


@mcp.tool()
def probe(key: str, validate_for: str | None = None) -> Any:
    """ffprobe + validacao opcional contra spec Meta Ads.

    Args:
      validate_for: 'meta_image_feed' | 'meta_image_story' |
                    'meta_video_feed' | 'meta_video_reels' | None.
                    None retorna so metadados.
    """
    result = _probe_internal(key)
    if "error" in result:
        return result
    if validate_for:
        return _validate_against_spec(result, validate_for)
    return result


# ============================================================
# Bridge para o Gestor (meta_ads_cli_mcp.create_creative)
# ============================================================

@mcp.tool()
def finalize_for_meta(b2_key: str, slug: str, description: str) -> Any:
    """Materializa midia finalizada para o Gestor publicar via create_creative.

    UNICO caminho que escreve em /root/.openclaw/workspace/_shared/creatives/.

    1. Roda probe + valida contra spec Meta (auto-detectada por aspecto/ext).
    2. Baixa do B2 para <_shared/creatives>/<slug>-<timestamp>.<ext>.
    3. Retorna dict completo no contrato Estrategista (path, dim, dur, fmt, desc).

    Args:
      b2_key: chave da midia pronta no B2 (geralmente em work/<slug>/...).
      slug: identificador final (vira parte do nome do arquivo).
      description: 1 frase descrevendo o conceito (passada a Estrategista).
    """
    err = _validate_key(b2_key)
    if err:
        return err
    err = _check_b2_config()
    if err:
        return err
    s = _slugify(slug)
    if not s:
        return {"error": "slug invalido"}
    if not description or not description.strip():
        return {"error": "description obrigatoria (1 frase descrevendo o conceito)"}

    ext = _ext_of(b2_key)
    if not ext:
        return {"error": "b2_key sem extensao"}
    is_video = ext in ("mp4", "mov", "webm", "mkv")
    is_image = ext in ("jpg", "jpeg", "png", "webp")
    if not (is_video or is_image):
        return {"error": f"extensao '{ext}' nao suportada para Meta Ads"}

    head = _b2_head(b2_key)
    if not head:
        return {"error": f"objeto nao existe no B2: {b2_key}"}

    probe_result = _probe_internal(b2_key)
    if "error" in probe_result:
        return probe_result

    width = probe_result.get("width")
    height = probe_result.get("height")

    validate_for = None
    if width and height:
        ratio = width / height
        if is_video:
            validate_for = "meta_video_reels" if ratio < 0.7 else "meta_video_feed"
        else:
            validate_for = "meta_image_story" if ratio < 0.7 else "meta_image_feed"
    validated = _validate_against_spec(probe_result, validate_for) if validate_for else probe_result

    FINAL_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"{s}-{_ts()}.{ext}"
    out_path = FINAL_LOCAL_DIR / out_name
    d_err = _b2_download(b2_key, out_path)
    if d_err:
        return d_err

    return {
        "path": str(out_path),
        "kind": "video" if is_video else "image",
        "format_name": probe_result.get("format_name"),
        "width": width,
        "height": height,
        "duration_seconds": probe_result.get("duration_seconds"),
        "size_bytes": probe_result.get("size_bytes"),
        "video_codec": probe_result.get("video_codec"),
        "audio_codec": probe_result.get("audio_codec"),
        "fps": probe_result.get("fps"),
        "description": description.strip(),
        "b2_key": b2_key,
        "valid_for_meta": validated.get("valid"),
        "violations": validated.get("violations", []),
        "validated_for": validated.get("validated_for"),
    }


if __name__ == "__main__":
    mcp.run()
