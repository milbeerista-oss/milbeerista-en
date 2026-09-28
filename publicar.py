#!/usr/bin/env python3
"""
milbeerista-en (versión gratuita, sin clave de Flickr)
Cada ejecución publica UNA reseña en Instagram (en inglés):
  1. Lee las páginas públicas de Flickr de pep_tf y busca la siguiente cerveza.
  2. Traduce la reseña al inglés (Gemini gratis, o Claude si hay clave).
  3. Ajusta la foto a 4:5 y la publica en milbeeristablog2.
  4. Guarda en state.json por dónde va.
Con DRY_RUN=1 solo muestra lo que publicaría.
"""
import csv
import html
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
from PIL import Image, ImageOps

# ---------------- Configuración ----------------
FLICKR_USER = "pep_tf"
START_DATE = "2023-01-01"        # primera fecha de subida a Flickr que entra en la cola
CATCHUP_UNTIL = "2025-07-01"     # los turnos extra solo publican fotos anteriores a esta fecha
IG_USER_ID = "17841471139169973"  # milbeeristablog2
IG_API = "https://graph.instagram.com/v23.0"
GEMINI_MODEL = "gemini-flash-latest"
GROQ_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
MAX_HASHTAGS = 30
MAX_FAILURES = 3
MAX_PAGES_PER_RUN = 40            # máximo de fotos de Flickr que mira en una ejecución
STATE_FILE = "state.json"
IDS_FILE = "flickr_ids.json"
LOG_FILE = "publicadas.csv"
IMAGES_DIR = "images"

DRY_RUN = os.environ.get("DRY_RUN") == "1"
EXTRA_SLOT = os.environ.get("EXTRA_SLOT") == "true"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IG_TOKEN = os.environ.get("IG_TOKEN", "")

# Flickr envía la página completa (100 fotos por página) a programas que no se hacen
# pasar por un navegador; a un navegador le manda solo 25 y carga el resto con JavaScript.
HEADERS = {
    "User-Agent": "milbeerista-bot/1.0 (+https://github.com)",
    "Accept-Language": "en-US,en;q=0.9",
}

TRANSLATION_PROMPT = """You translate Catalan craft-beer reviews into natural English for an Instagram account.
Rules:
- Keep beer names, brewery names, place names, hop varieties, malt names and yeast strains exactly as written.
- Keep numbers, IBUs and abbreviations as they are, but use a decimal point in numbers (6,7% -> 6.7%).
- Translate every Catalan word, including short phrases such as "De Blanes" -> "From Blanes".
- Keep the author's concise tasting-note style. Do not add or remove information.
- Output only the English translation, with no quotes, notes or preamble."""


# ---------------- Flickr (páginas públicas) ----------------
def fetch(url, tries=3):
    for i in range(tries):
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            return r.text
        if r.status_code == 404:
            return None
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"Flickr no responde ({r.status_code}): {url}")


def ids_in_page(n, tries=3):
    """IDs de las fotos de una página del photostream (reintenta si sale vacía)."""
    for i in range(tries):
        page = fetch(f"https://www.flickr.com/photos/{FLICKR_USER}/page{n}") or ""
        # Solo unas 20 fotos por página salen como imagen; el resto viene en los datos
        # internos de la página (con barras escapadas \/ o como "id":"...").
        found = {int(x) for x in re.findall(r"staticflickr\.com\\?/\d+\\?/(\d{6,})_[0-9a-f]{6,}_", page)}
        found |= {int(x) for x in re.findall(r'"id"\s*:\s*"(\d{9,12})"', page)}
        if found:
            last = max([int(x) for x in re.findall(rf"/photos/{FLICKR_USER}/page(\d+)", page)] or [n])
            return found, last
        time.sleep(10 * (i + 1))
    return set(), n


def update_ids(ids):
    """Primera vez: recorre todo el photostream. Después: solo las páginas nuevas."""
    known = set(ids)
    first_run = not ids
    found, last_page = ids_in_page(1)
    print(f"Página 1 de Flickr: {len(found)} fotos (de {last_page} páginas)")
    known |= found
    n = 1
    while n < last_page:
        if not first_run and found and not (found - set(ids)):
            break
        n += 1
        time.sleep(1)
        found, maybe_last = ids_in_page(n)
        last_page = max(last_page, maybe_last)
        if not found:
            print(f"Aviso: la página {n} de Flickr ha salido vacía.")
        known |= found
    return sorted(known)


def meta_tags(page):
    tags = {}
    for tag in re.findall(r"<meta\s[^>]*>", page):
        attrs = dict(re.findall(r'([\w:-]+)="([^"]*)"', tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            tags[key] = html.unescape(attrs["content"])
    return tags


def photo_info(pid):
    page = fetch(f"https://www.flickr.com/photos/{FLICKR_USER}/{pid}/")
    if not page:
        return None
    meta = meta_tags(page)
    m = re.search(r'"datePosted"\s*:\s*"?(\d{9,11})', page)
    if m:
        uploaded = datetime.fromtimestamp(int(m.group(1)), timezone.utc).date()
    else:
        m = re.search(r"Uploaded on\s*(?:<[^>]+>\s*)*([A-Z][a-z]+ \d{1,2}, \d{4})", page)
        if not m:
            raise RuntimeError(f"No encuentro la fecha de subida de la foto {pid}")
        uploaded = datetime.strptime(m.group(1), "%B %d, %Y").date()
    return {
        "id": pid,
        "title": meta.get("og:title", "").strip(),
        "description": meta.get("og:description", "").strip(),
        "image": meta.get("og:image", ""),
        "uploaded": uploaded.isoformat(),
    }


def is_beer(info):
    return bool(re.search(r"#(craft)?beer\b", info["description"], re.I))


def find_start(ids, start_date):
    """Búsqueda binaria de la primera foto subida en start_date o después."""
    lo, hi = 0, len(ids)
    while lo < hi:
        mid = (lo + hi) // 2
        info = photo_info(ids[mid])
        time.sleep(1)
        if info is None or info["uploaded"] < start_date:
            lo = mid + 1
        else:
            hi = mid
    return ids[lo - 1] if lo > 0 else 0


def next_beer(state, ids):
    """Devuelve la siguiente cerveza de la cola, saltando fotos que no son reseñas."""
    checked = 0
    for pid in ids:
        if pid <= state["last_id"]:
            continue
        if state["failures"].get(str(pid), 0) >= MAX_FAILURES:
            print(f"Saltando {pid}: falló {MAX_FAILURES} veces.")
            state["last_id"] = pid
            continue
        if checked >= MAX_PAGES_PER_RUN:
            print("Muchas fotos que no son cervezas seguidas; sigo en la próxima ejecución.")
            return None
        info = photo_info(pid)
        checked += 1
        time.sleep(1)
        if info and is_beer(info) and info["image"]:
            return info
        state["last_id"] = pid  # no es una reseña: se avanza sin publicar
    return None


# ---------------- Texto ----------------
def split_hashtags(text):
    m = re.search(r"(?:^|\s)#\w", text)
    if not m:
        return text.strip(), []
    body, tags_part = text[: m.start()].strip(), text[m.start():]
    tags, seen = [], set()
    for tag in re.findall(r"#[^\s#]+", tags_part):
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return body, tags[:MAX_HASHTAGS]


def translate_gemini(body):
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        params={"key": GEMINI_API_KEY},
        json={
            "system_instruction": {"parts": [{"text": TRANSLATION_PROMPT}]},
            "contents": [{"parts": [{"text": body}]}],
        },
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini: {r.status_code} {r.text[:300]}")
    parts = r.json()["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip()


def translate_groq(body):
    """Groq (gratis, sin tarjeta). Prueba varios modelos por si alguno deja de existir."""
    last_error = ""
    for model in GROQ_MODELS:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": TRANSLATION_PROMPT},
                         {"role": "user", "content": body}],
            "temperature": 0.2,
        }
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                          headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                          json=payload, timeout=90)
        if r.ok:
            text = (r.json()["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
        last_error = f"{model}: {r.status_code} {r.text[:200]}"
    raise RuntimeError(f"Groq: {last_error}")


def translate_claude(body):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
        json={"model": CLAUDE_MODEL, "max_tokens": 1024, "system": TRANSLATION_PROMPT,
              "messages": [{"role": "user", "content": body}]},
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"Claude: {r.status_code} {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json()["content"] if b["type"] == "text").strip()


def chunks(text, max_bytes=450):
    """Parte el texto por frases en trozos que acepta MyMemory (máx. 500 bytes)."""
    out, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        candidate = f"{current} {sentence}".strip()
        if len(candidate.encode()) <= max_bytes:
            current = candidate
            continue
        if current:
            out.append(current)
        while len(sentence.encode()) > max_bytes:          # frase larguísima: cortar por palabras
            cut = sentence[:max_bytes // 2].rsplit(" ", 1)[0] or sentence[:max_bytes // 2]
            out.append(cut)
            sentence = sentence[len(cut):].strip()
        current = sentence
    if current:
        out.append(current)
    return out


def translate_mymemory(body):
    """Traductor gratuito sin clave (MyMemory). Calidad algo menor que una IA."""
    pieces = []
    for piece in chunks(body):
        r = requests.get("https://api.mymemory.translated.net/get",
                         params={"q": piece, "langpair": "ca|en"}, timeout=60)
        data = r.json() if r.ok else {}
        if data.get("responseStatus") != 200:
            raise RuntimeError(f"MyMemory: {r.status_code} {str(data)[:300]}")
        pieces.append(html.unescape(data["responseData"]["translatedText"]).strip())
        time.sleep(1)
    return re.sub(r"(\d),(\d)", r"\1.\2", " ".join(pieces))


def translate(body):
    """Prueba los traductores en orden: Gemini, Groq, Claude y, si no, MyMemory (sin clave)."""
    if not body:
        return ""
    for key, fn in ((GEMINI_API_KEY, translate_gemini), (GROQ_API_KEY, translate_groq),
                    (ANTHROPIC_API_KEY, translate_claude)):
        if key:
            try:
                return fn(body)
            except Exception as e:
                print(f"Aviso: falló la traducción ({e}). Pruebo la siguiente opción.")
    print("Traduciendo con MyMemory (gratuito).")
    return translate_mymemory(body)


def build_caption(title, body_en, tags):
    title = (title or "").strip()
    if title and body_en:
        sep = " " if title.endswith((".", "!", "?")) else ". "
        caption = f"{title}{sep}{body_en}"
    else:
        caption = title or body_en
    if tags:
        caption += "\n\n" + " ".join(tags)
    return caption[:2200]


# ---------------- Imagen ----------------
def prepare_image(url, path):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(r.content))).convert("RGB")
    w, h = img.size
    if w / h < 0.8:
        nh = round(w / 0.8)
        top = (h - nh) // 2
        img = img.crop((0, top, w, top + nh))
    elif w / h > 1.91:
        nw = round(h * 1.91)
        left = (w - nw) // 2
        img = img.crop((left, 0, left + nw, h))
    if img.width > 1440:
        img = img.resize((1440, round(img.height * 1440 / img.width)), Image.LANCZOS)
    img.save(path, "JPEG", quality=92)
    return img.size


def wait_for_url(url, tries=24):
    for _ in range(tries):
        if requests.head(url, timeout=15).status_code == 200:
            return
        time.sleep(5)
    raise RuntimeError(f"La imagen no está accesible: {url}")


# ---------------- Instagram ----------------
def ig_call(method, path, **data):
    data["access_token"] = IG_TOKEN
    if method == "post":
        r = requests.post(f"{IG_API}/{path}", data=data, timeout=60)
    else:
        r = requests.get(f"{IG_API}/{path}", params=data, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram {path}: {r.status_code} {r.text}")
    return r.json()


def publish(image_url, caption):
    container = ig_call("post", f"{IG_USER_ID}/media", image_url=image_url, caption=caption)["id"]
    for _ in range(36):
        status = ig_call("get", container, fields="status_code").get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram no pudo procesar la imagen (estado {status})")
        time.sleep(5)
    else:
        raise RuntimeError("Instagram tardó demasiado en procesar la imagen")
    return ig_call("post", f"{IG_USER_ID}/media_publish", creation_id=container)["id"]


# ---------------- Git / archivos ----------------
def git(*args):
    subprocess.run(["git", *args], check=True)


def commit_and_push(message):
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        git("commit", "-m", message)
        git("push")


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write("\n")


def log_published(info, media_id):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["fecha", "flickr_id", "subida_flickr", "titulo", "instagram_id"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    info["id"], info["uploaded"], info["title"], media_id])


# ---------------- Principal ----------------
def main():
    state = load_json(STATE_FILE, {})
    state.setdefault("failures", {})
    ids = update_ids(load_json(IDS_FILE, []))
    print(f"Fotos conocidas en Flickr: {len(ids)}")

    if not state.get("last_id"):
        print(f"Buscando la primera foto subida a partir del {START_DATE}...")
        state["last_id"] = find_start(ids, START_DATE)

    if not DRY_RUN:
        git("config", "user.name", "milbeerista-bot")
        git("config", "user.email", "bot@users.noreply.github.com")

    def save_progress(message):
        save_json(STATE_FILE, state)
        save_json(IDS_FILE, ids)
        if not DRY_RUN:
            commit_and_push(message)

    info = next_beer(state, ids)
    if not info:
        print("No hay cervezas nuevas en la cola.")
        save_progress("Actualizar cola")
        return

    if EXTRA_SLOT and info["uploaded"] >= CATCHUP_UNTIL:
        print("Turno extra: la puesta al día ya ha terminado, no se publica en este turno.")
        save_progress("Actualizar cola")
        return

    body, tags = split_hashtags(info["description"])
    caption = build_caption(info["title"], translate(body), tags)
    print(f"Siguiente: {info['id']} (subida {info['uploaded']})\n----- TEXTO -----\n{caption}\n-----------------")

    if DRY_RUN:
        size = prepare_image(info["image"], "/tmp/prueba.jpg")
        print(f"Imagen preparada: {size[0]}x{size[1]} px\nMODO PRUEBA: no se ha publicado nada.")
        return

    os.makedirs(IMAGES_DIR, exist_ok=True)
    image_path = f"{IMAGES_DIR}/{info['id']}.jpg"
    prepare_image(info["image"], image_path)
    save_progress(f"Imagen temporal: {info['title']}")
    repo = os.environ["GITHUB_REPOSITORY"]
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    image_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{image_path}"

    try:
        wait_for_url(image_url)
        media_id = publish(image_url, caption)
    except Exception:
        key = str(info["id"])
        state["failures"][key] = state["failures"].get(key, 0) + 1
        os.remove(image_path)
        save_progress(f"Fallo al publicar: {info['title']}")
        raise

    state["last_id"] = info["id"]
    state["failures"].pop(str(info["id"]), None)
    os.remove(image_path)
    log_published(info, media_id)
    save_progress(f"Publicada: {info['title']}")
    print(f"¡Publicada! Instagram id {media_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
