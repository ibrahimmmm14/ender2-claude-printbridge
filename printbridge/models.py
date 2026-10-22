"""
models.py - Download 3D models (STL / 3MF / OBJ / ZIP) from a URL.

Supports direct file links, Thingiverse things (".../thing:123" -> zip),
Printables model pages (public GraphQL API, best effort) and generic pages
that link to model files.
"""
from __future__ import annotations

import io
import os
import re
import urllib.parse
import zipfile
from typing import Dict, List, Optional

import requests

MODEL_EXT = (".stl", ".3mf", ".obj")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PrintBridge/1.0"}


def _safe_name(name: str) -> str:
    name = urllib.parse.unquote(name.split("?")[0].split("#")[0])
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", os.path.basename(name)).strip() or "model.stl"
    return name


def _save(content: bytes, name: str, dest_dir: str) -> List[str]:
    os.makedirs(dest_dir, exist_ok=True)
    out: List[str] = []
    if name.lower().endswith(".zip") or content[:2] == b"PK" and not name.lower().endswith(".3mf"):
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.lower().endswith(MODEL_EXT):
                    continue
                if info.file_size > 300 * 1024 * 1024:
                    continue
                target = os.path.join(dest_dir, _safe_name(info.filename))
                with z.open(info) as src, open(target, "wb") as dst:
                    dst.write(src.read())
                out.append(target)
        return out
    target = os.path.join(dest_dir, _safe_name(name))
    with open(target, "wb") as f:
        f.write(content)
    return [target]


def _fetch(url: str, session: requests.Session) -> requests.Response:
    r = session.get(url, headers=UA, timeout=60, allow_redirects=True)
    r.raise_for_status()
    return r


def _printables(url: str, session: requests.Session, dest_dir: str) -> List[str]:
    m = re.search(r"/model/(\d+)", url)
    if not m:
        return []
    model_id = m.group(1)
    api = "https://api.printables.com/graphql/"
    q = {"operationName": "PrintFiles", "variables": {"id": model_id},
         "query": "query PrintFiles($id: ID!) { print(id: $id) { id name stls { id name fileSize } } }"}
    r = session.post(api, json=q, headers={**UA, "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    stls = (((r.json() or {}).get("data") or {}).get("print") or {}).get("stls") or []
    out: List[str] = []
    for f in stls[:10]:
        mut = {"operationName": "GetDownloadLink",
               "variables": {"id": f["id"], "modelId": model_id, "fileType": "stl", "source": "model_detail"},
               "query": "mutation GetDownloadLink($id: ID!, $modelId: ID!, $fileType: DownloadFileTypeEnum!, "
                        "$source: DownloadSourceEnum!) { getDownloadLink(id: $id, printId: $modelId, "
                        "fileType: $fileType, source: $source) { ok output { link } } }"}
        d = session.post(api, json=mut, headers={**UA, "Content-Type": "application/json"}, timeout=30).json()
        link = ((((d or {}).get("data") or {}).get("getDownloadLink") or {}).get("output") or {}).get("link")
        if link:
            out += _save(_fetch(link, session).content, f.get("name") or "model.stl", dest_dir)
    return out


def download_model(url: str, dest_dir: str) -> Dict[str, object]:
    session = requests.Session()
    url = url.strip()
    files: List[str] = []
    errors: List[str] = []
    path = urllib.parse.urlparse(url).path.lower()
    try:
        if path.endswith(MODEL_EXT + (".zip",)):
            r = _fetch(url, session)
            files = _save(r.content, os.path.basename(path), dest_dir)
        elif "thingiverse.com" in url:
            m = re.search(r"thing:(\d+)", url)
            if m:
                r = _fetch("https://www.thingiverse.com/thing:%s/zip" % m.group(1), session)
                files = _save(r.content, "thing_%s.zip" % m.group(1), dest_dir)
        elif "printables.com" in url:
            files = _printables(url, session, dest_dir)
    except Exception as e:  # keep going with the generic method
        errors.append("%s: %s" % (type(e).__name__, e))
    if not files:
        try:
            r = _fetch(url, session)
            ctype = r.headers.get("content-type", "")
            if "html" not in ctype:
                name = re.findall(r'filename="?([^";]+)', r.headers.get("content-disposition", "")) or [path or "model.stl"]
                files = _save(r.content, name[0], dest_dir)
            else:
                links = re.findall(r'href="([^"]+?\.(?:stl|3mf|obj|zip)(?:\?[^"]*)?)"', r.text, re.I)
                for link in links[:5]:
                    full = urllib.parse.urljoin(url, link)
                    try:
                        files += _save(_fetch(full, session).content, link, dest_dir)
                    except Exception as e:
                        errors.append("%s: %s" % (full, e))
                    if files:
                        break
        except Exception as e:
            errors.append("%s: %s" % (type(e).__name__, e))
    return {"files": files, "errors": errors,
            "hint": None if files else "Could not download automatically. Open the page, download the STL "
                                       "and give me its file path, or give me a direct .stl/.3mf/.zip link."}
