#!/usr/bin/env python3
"""
hr-talent-coordinator MCP server.

Exposes an Agent Skills directory over the Model Context Protocol so any
MCP-compatible client can discover and load the skill.

Zero dependencies - standard library only. Transport: stdio (JSON-RPC 2.0).

Usage:
    python server.py [--skills-dir PATH]

If --skills-dir is omitted, ./skills next to this file is used.
Environment variable SKILLS_DIR is also honoured (flag wins).
"""

import base64
import html
import json
import mimetypes
import os
import socketserver
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SERVER_NAME = "hr-talent-coordinator"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}
MAX_TEXT_BYTES = 400_000

TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".tsv",
                 ".py", ".js", ".ts", ".sh", ".sql", ".html", ".xml", ".toml", ".ini"}


def log(msg):
    """Diagnostics go to stderr. stdout carries protocol traffic only."""
    print(f"[{SERVER_NAME}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Skill discovery
# --------------------------------------------------------------------------

def resolve_skills_dir(argv):
    path = None
    for i, a in enumerate(argv):
        if a == "--skills-dir" and i + 1 < len(argv):
            path = argv[i + 1]
        elif a.startswith("--skills-dir="):
            path = a.split("=", 1)[1]
    if not path:
        path = os.environ.get("SKILLS_DIR")
    if not path:
        path = Path(__file__).resolve().parent / "skills"
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        log(f"WARNING: skills directory not found: {p}")
    return p


def parse_frontmatter(text):
    """Minimal YAML frontmatter reader: only the flat keys a SKILL.md needs."""
    meta, body = {}, text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw = text[3:end]
            body = text[end + 4:].lstrip("\n")
            key, buf = None, []
            for line in raw.splitlines():
                if not line.strip() or line.strip().startswith("#"):
                    continue
                if ":" in line and not line.startswith((" ", "\t")):
                    if key:
                        meta[key] = " ".join(buf).strip().strip('"').strip("'")
                    key, rest = line.split(":", 1)
                    key = key.strip()
                    buf = [rest.strip()]
                else:
                    buf.append(line.strip())
            if key:
                meta[key] = " ".join(buf).strip().strip('"').strip("'")
    return meta, body


def discover(skills_dir):
    out = {}
    if not skills_dir.is_dir():
        return out
    for entry in sorted(skills_dir.iterdir()):
        md = entry / "SKILL.md"
        if not (entry.is_dir() and md.is_file()):
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception as e:
            log(f"skip {entry.name}: {e}")
            continue
        meta, body = parse_frontmatter(text)
        out[meta.get("name") or entry.name] = {
            "name": meta.get("name") or entry.name,
            "description": meta.get("description", ""),
            "directory": str(entry.resolve()),
            "skill_md_path": str(md.resolve()),
            "metadata": meta,
            "content": text,
            "body": body,
        }
    return out


def list_files(skill):
    root = Path(skill["directory"])
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts):
            files.append({
                "relative_path": str(p.relative_to(root)).replace("\\", "/"),
                "absolute_path": str(p.resolve()),
                "bytes": p.stat().st_size,
            })
    return files


def safe_join(skill, rel):
    root = Path(skill["directory"]).resolve()
    target = (root / rel).resolve()
    if root not in target.parents and target != root:
        raise ValueError("path escapes the skill directory")
    if not target.is_file():
        raise ValueError(f"file not found: {rel}")
    return target


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "list_skills",
        "description": ("List every Agent Skill available on this server, with its name, "
                        "description and absolute directory path. Call this first to discover "
                        "what the server offers."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_skill",
        "description": ("Load a skill's full SKILL.md instructions. Returns the instruction text "
                        "plus the absolute path to the skill directory so bundled files can be "
                        "resolved. Call this when a task matches a skill's description."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name from list_skills."}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "list_skill_files",
        "description": ("List the bundled files inside a skill directory (templates, references, "
                        "scripts) with relative and absolute paths."),
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Skill name."}},
            "required": ["name"], "additionalProperties": False,
        },
    },
    {
        "name": "read_skill_file",
        "description": ("Read one bundled file from a skill directory. Text files return their "
                        "content; binary files (xlsx, docx, pdf) return base64 plus the absolute "
                        "path. Clients with their own filesystem access should prefer the path."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name."},
                "path": {"type": "string", "description": "Path relative to the skill directory, e.g. assets/Offer_Letter_Template.docx"},
            },
            "required": ["name", "path"], "additionalProperties": False,
        },
    },
]


def call_tool(skills, tool, args):
    if tool == "list_skills":
        payload = [{"name": s["name"], "description": s["description"], "directory": s["directory"]}
                   for s in skills.values()]
        return json.dumps({"skills": payload, "count": len(payload)}, indent=2)

    if tool in ("get_skill", "list_skill_files", "read_skill_file"):
        name = args.get("name")
        skill = skills.get(name)
        if not skill:
            avail = ", ".join(skills) or "(none)"
            raise ValueError(f"unknown skill '{name}'. Available: {avail}")

        if tool == "get_skill":
            return json.dumps({
                "name": skill["name"],
                "description": skill["description"],
                "directory": skill["directory"],
                "skill_md_path": skill["skill_md_path"],
                "bundled_files": [f["relative_path"] for f in list_files(skill)],
                "instructions": skill["content"],
            }, indent=2)

        if tool == "list_skill_files":
            return json.dumps({"name": skill["name"], "directory": skill["directory"],
                               "files": list_files(skill)}, indent=2)

        target = safe_join(skill, args.get("path", ""))
        data = target.read_bytes()
        is_text = target.suffix.lower() in TEXT_SUFFIXES
        res = {"name": skill["name"],
               "path": str(target.relative_to(Path(skill["directory"]))).replace("\\", "/"),
               "absolute_path": str(target), "bytes": len(data),
               "mime_type": mimetypes.guess_type(target.name)[0] or "application/octet-stream"}
        if is_text and len(data) <= MAX_TEXT_BYTES:
            res["encoding"] = "utf-8"
            res["content"] = data.decode("utf-8", errors="replace")
        else:
            res["encoding"] = "base64"
            res["note"] = "Binary or oversized file. Prefer opening absolute_path directly."
            res["content_base64"] = base64.b64encode(data).decode("ascii")
        return json.dumps(res, indent=2)

    raise ValueError(f"unknown tool: {tool}")


# --------------------------------------------------------------------------
# JSON-RPC dispatch
# --------------------------------------------------------------------------

def handle(req, state):
    method = req.get("method")
    params = req.get("params") or {}
    skills = state["skills"]

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        state["initialized"] = True
        return {"protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False},
                                 "prompts": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": ("Agent Skills served over MCP. Call list_skills to discover, then "
                                 "get_skill to load a skill's instructions before starting the task.")}

    if method == "ping":
        return {}

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            return {"content": [{"type": "text", "text": call_tool(skills, name, args)}],
                    "isError": False}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}

    if method == "prompts/list":
        return {"prompts": [{"name": s["name"],
                             "description": (s["description"] or "")[:300],
                             "arguments": [{"name": "task",
                                            "description": "What you need the agent to do.",
                                            "required": False}]}
                            for s in skills.values()]}

    if method == "prompts/get":
        name = params.get("name")
        skill = skills.get(name)
        if not skill:
            raise ValueError(f"unknown prompt '{name}'")
        task = (params.get("arguments") or {}).get("task", "")
        text = skill["body"] or skill["content"]
        if task:
            text += f"\n\n---\n\nTask: {task}"
        text += f"\n\n(Bundled files for this skill are in: {skill['directory']})"
        return {"description": skill["description"],
                "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    if method == "resources/list":
        res = []
        for s in skills.values():
            for f in list_files(s):
                res.append({"uri": f"skill://{s['name']}/{f['relative_path']}",
                            "name": f"{s['name']}/{f['relative_path']}",
                            "description": f"Bundled file from the {s['name']} skill",
                            "mimeType": mimetypes.guess_type(f["relative_path"])[0] or "application/octet-stream"})
        return {"resources": res}

    if method == "resources/read":
        uri = params.get("uri", "")
        if not uri.startswith("skill://"):
            raise ValueError(f"unsupported uri: {uri}")
        rest = uri[len("skill://"):]
        sname, _, rel = rest.partition("/")
        skill = skills.get(sname)
        if not skill:
            raise ValueError(f"unknown skill '{sname}'")
        target = safe_join(skill, rel)
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() in TEXT_SUFFIXES and len(data) <= MAX_TEXT_BYTES:
            return {"contents": [{"uri": uri, "mimeType": mime,
                                  "text": data.decode("utf-8", errors="replace")}]}
        return {"contents": [{"uri": uri, "mimeType": mime,
                              "blob": base64.b64encode(data).decode("ascii")}]}

    raise LookupError(f"method not found: {method}")


def dispatch(req, state):
    """Shared JSON-RPC handling for both stdio and HTTP transports.

    Returns (response_dict_or_None, http_status). response is None for
    notifications (no id), which callers must translate to "no body".
    """
    if "id" not in req or req.get("id") is None:
        if req.get("method") == "notifications/initialized":
            log("client initialized")
        return None, 202

    try:
        result = handle(req, state)
        return {"jsonrpc": "2.0", "id": req["id"], "result": result}, 200
    except LookupError as e:
        return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": str(e)}}, 200
    except Exception as e:
        return {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32603, "message": str(e)}}, 200


# --------------------------------------------------------------------------
# Streamable HTTP transport (remote deployment)
# --------------------------------------------------------------------------

LANDING_PAGE_SCRIPT = """
(function () {
  const skills = JSON.parse(document.getElementById('skills-data').textContent);
  const skillNames = skills.map(function (s) { return s.name; });
  const TOKEN_KEY = 'hrmcp_token';
  const tokenInput = document.getElementById('token-input');
  const mcpBase = window.location.origin + '/mcp';
  const urlDisplay = document.getElementById('connect-url');
  const copyBtn = document.getElementById('copy-url-btn');

  tokenInput.value = sessionStorage.getItem(TOKEN_KEY) || '';

  function updateConnectUrl() {
    const t = tokenInput.value.trim();
    if (!t) {
      urlDisplay.textContent = mcpBase + '?token=<pega-tu-token-arriba>';
      copyBtn.disabled = true;
    } else {
      urlDisplay.textContent = mcpBase + '?token=' + t;
      copyBtn.disabled = false;
    }
  }
  tokenInput.addEventListener('input', function () {
    sessionStorage.setItem(TOKEN_KEY, tokenInput.value);
    updateConnectUrl();
  });
  updateConnectUrl();

  copyBtn.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(urlDisplay.textContent);
      copyBtn.textContent = 'Copiado';
      setTimeout(function () { copyBtn.textContent = 'Copiar'; }, 1500);
    } catch (e) {
      alert('No se pudo copiar automaticamente. Selecciona el texto y copia manualmente.');
    }
  });

  function fillSelect(select, items) {
    select.innerHTML = '';
    items.forEach(function (v) {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = v;
      select.appendChild(opt);
    });
  }
  document.querySelectorAll('.skill-select').forEach(function (sel) { fillSelect(sel, skillNames); });

  async function callTool(name, args) {
    const t = tokenInput.value.trim();
    if (!t) { throw new Error('pega tu token arriba primero'); }
    const res = await fetch(mcpBase, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + t },
      body: JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method: 'tools/call', params: { name: name, arguments: args } })
    });
    if (res.status === 401) { throw new Error('401: token invalido'); }
    const data = await res.json();
    if (data.error) { throw new Error(data.error.message || 'error MCP'); }
    const content = data.result && data.result.content;
    if (content && content[0] && content[0].text) {
      try { return JSON.parse(content[0].text); } catch (e) { return content[0].text; }
    }
    return data.result;
  }

  function showResult(el, value) {
    if (value && typeof value === 'object' && value.encoding === 'base64' && value.content_base64) {
      const preview = value.content_base64.slice(0, 120) + '... (' + value.content_base64.length + ' caracteres base64, truncado)';
      value = Object.assign({}, value, { content_base64: preview });
    }
    el.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  }

  async function run(btnId, outId, fn) {
    const btn = document.getElementById(btnId);
    const out = document.getElementById(outId);
    btn.addEventListener('click', async function () {
      out.textContent = 'Cargando...';
      try { showResult(out, await fn()); }
      catch (e) { out.textContent = 'Error: ' + e.message; }
    });
  }

  run('btn-list-skills', 'out-list-skills', function () { return callTool('list_skills', {}); });

  run('btn-get-skill', 'out-get-skill', function () {
    return callTool('get_skill', { name: document.getElementById('sel-get-skill').value });
  });

  run('btn-list-files', 'out-list-files', async function () {
    const name = document.getElementById('sel-list-files').value;
    const result = await callTool('list_skill_files', { name: name });
    if (result && result.files) {
      fillSelect(document.getElementById('sel-read-file-path'), result.files.map(function (f) { return f.relative_path; }));
    }
    return result;
  });

  run('btn-read-file', 'out-read-file', function () {
    const name = document.getElementById('sel-read-file-name').value;
    const path = document.getElementById('sel-read-file-path').value;
    if (!path) { throw new Error('ejecuta "Listar archivos" primero para elegir una ruta'); }
    return callTool('read_skill_file', { name: name, path: path });
  });
})();
"""


def render_landing_page(state, base_url):
    mcp_url = base_url.rstrip("/") + "/mcp"
    skills = list(state["skills"].values())
    cards = "".join(
        f"""
        <li class="skill">
          <h3>{html.escape(s['name'])}</h3>
          <p>{html.escape(s['description'])}</p>
        </li>"""
        for s in skills
    ) or '<li class="skill empty">No hay skills cargadas.</li>'
    skills_json = json.dumps([{"name": s["name"]} for s in skills])

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{SERVER_NAME}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0; padding: 3rem 1.5rem 4rem; background: #0b0d12; color: #e7e9ee;
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  main {{ max-width: 720px; margin: 0 auto; }}
  .badge {{
    display: inline-block; font-size: .75rem; letter-spacing: .04em; text-transform: uppercase;
    color: #7ee787; background: rgba(126,231,135,.12); border: 1px solid rgba(126,231,135,.35);
    border-radius: 999px; padding: .2rem .7rem; margin-bottom: 1rem;
  }}
  h1 {{ font-size: 1.7rem; margin: 0 0 .4rem; }}
  .sub {{ color: #9aa4b2; margin: 0 0 2rem; }}
  h2 {{ font-size: 1rem; text-transform: uppercase; letter-spacing: .04em; color: #9aa4b2; margin: 2.2rem 0 .8rem; }}
  ul.skill-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: .8rem; }}
  .skill {{
    background: #12151c; border: 1px solid #232733; border-radius: 12px; padding: 1rem 1.2rem;
  }}
  .skill h3 {{ margin: 0 0 .35rem; font-size: 1.02rem; }}
  .skill p {{ margin: 0; color: #b3bac6; font-size: .92rem; }}
  .skill.empty {{ color: #9aa4b2; }}
  code, pre {{
    background: #12151c; border: 1px solid #232733; border-radius: 8px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem;
  }}
  code {{ padding: .15rem .4rem; }}
  pre {{ padding: .9rem 1rem; overflow-x: auto; white-space: pre-wrap; word-break: break-word; }}
  ol {{ padding-left: 1.2rem; color: #d6dae2; }}
  ol li {{ margin-bottom: .5rem; }}
  footer {{ margin-top: 3rem; color: #6b7280; font-size: .82rem; }}
  a {{ color: #7dabff; }}

  .panel {{ background: #12151c; border: 1px solid #232733; border-radius: 12px; padding: 1.2rem; margin-bottom: 1rem; }}
  .row {{ display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }}
  input[type="password"], select {{
    background: #0b0d12; color: #e7e9ee; border: 1px solid #2b3040; border-radius: 8px;
    padding: .5rem .7rem; font-size: .9rem; flex: 1; min-width: 180px;
  }}
  button {{
    background: #2563eb; color: #fff; border: none; border-radius: 8px; padding: .5rem 1rem;
    font-size: .88rem; cursor: pointer; white-space: nowrap;
  }}
  button:disabled {{ background: #2b3040; cursor: not-allowed; color: #6b7280; }}
  button:hover:not(:disabled) {{ background: #3b76f0; }}
  .tool-card {{ border-top: 1px solid #232733; padding-top: 1rem; margin-top: 1rem; }}
  .tool-card:first-of-type {{ border-top: none; margin-top: 0; padding-top: 0; }}
  .tool-card h3 {{ margin: 0 0 .3rem; font-size: .95rem; }}
  .tool-card .desc {{ margin: 0 0 .6rem; color: #9aa4b2; font-size: .85rem; }}
  .tool-card pre {{ margin: .6rem 0 0; max-height: 260px; overflow-y: auto; font-size: .8rem; }}
  .hint {{ color: #6b7280; font-size: .8rem; margin: .5rem 0 0; }}
</style>
</head>
<body>
<main>
  <span class="badge">MCP server &middot; en linea</span>
  <h1>{SERVER_NAME}</h1>
  <p class="sub">Sirve Agent Skills por Model Context Protocol (Streamable HTTP). Version {SERVER_VERSION}.</p>

  <h2>Skills disponibles</h2>
  <ul class="skill-list">{cards}
  </ul>

  <h2>Conexion rapida</h2>
  <div class="panel">
    <div class="row">
      <input type="password" id="token-input" placeholder="Pega tu MCP_AUTH_TOKEN aqui" autocomplete="off">
    </div>
    <p class="hint">El token no se envia a ningun lado salvo a este mismo servidor; solo vive en esta pestana (sessionStorage).</p>
    <div class="row" style="margin-top:.8rem">
      <code id="connect-url" style="flex:1; min-width:220px; word-break:break-all;"></code>
      <button id="copy-url-btn" disabled>Copiar</button>
    </div>
    <p class="hint">Pegala en Claude: Settings &rarr; Conectores &rarr; Agregar conector personalizado &rarr; URL del servidor MCP remoto.</p>
  </div>

  <h2>Probar el servidor</h2>
  <div class="panel">
    <div class="tool-card">
      <h3>list_skills</h3>
      <p class="desc">Lista todas las skills cargadas en este servidor.</p>
      <button id="btn-list-skills">Ejecutar</button>
      <pre id="out-list-skills"></pre>
    </div>
    <div class="tool-card">
      <h3>get_skill</h3>
      <p class="desc">Devuelve las instrucciones completas (SKILL.md) de una skill.</p>
      <div class="row">
        <select id="sel-get-skill" class="skill-select"></select>
        <button id="btn-get-skill">Ejecutar</button>
      </div>
      <pre id="out-get-skill"></pre>
    </div>
    <div class="tool-card">
      <h3>list_skill_files</h3>
      <p class="desc">Lista los archivos incluidos en una skill (rutas y tamanos).</p>
      <div class="row">
        <select id="sel-list-files" class="skill-select"></select>
        <button id="btn-list-files">Ejecutar</button>
      </div>
      <pre id="out-list-files"></pre>
    </div>
    <div class="tool-card">
      <h3>read_skill_file</h3>
      <p class="desc">Lee un archivo. Ejecuta list_skill_files primero para elegir la ruta.</p>
      <div class="row">
        <select id="sel-read-file-name" class="skill-select"></select>
        <select id="sel-read-file-path"><option value="">(ejecuta list_skill_files primero)</option></select>
        <button id="btn-read-file">Ejecutar</button>
      </div>
      <pre id="out-read-file"></pre>
    </div>
  </div>

  <footer>
    <a href="/health">/health</a> para el estado del servicio.
  </footer>
</main>
<script type="application/json" id="skills-data">{skills_json}</script>
<script>{LANDING_PAGE_SCRIPT}</script>
</body>
</html>"""


def run_http_server(state, host, port, auth_token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

        def log_message(self, fmt, *args):
            log("%s - %s" % (self.address_string(), fmt % args))

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, Mcp-Session-Id, Accept")

        def _send_empty(self, status, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self._cors()
            self.end_headers()

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, status, html):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not auth_token:
                return True
            if self.headers.get("Authorization", "") == f"Bearer {auth_token}":
                return True
            query = urllib.parse.urlsplit(self.path).query
            return urllib.parse.parse_qs(query).get("token", [None])[0] == auth_token

        def do_OPTIONS(self):
            self._send_empty(204)

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/health":
                self._send_json(200, {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})
                return
            if path == "":
                scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
                base_url = f"{scheme}://{self.headers.get('Host', f'{host}:{port}')}"
                self._send_html(200, render_landing_page(state, base_url))
                return
            if path == "/mcp":
                # Streamable HTTP allows GET to open an SSE stream; this server
                # doesn't send server-initiated messages, so per spec reply 405
                # (not 404) telling the client to fall back to POST-only mode.
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                self._send_empty(405, {"Allow": "POST, OPTIONS"})
                return
            self._send_empty(404)

        def do_DELETE(self):
            # Streamable HTTP allows DELETE to end a session; this server is
            # stateless (no sessions to end), so reply 405 rather than 501.
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/mcp":
                if not self._authorized():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                self._send_empty(405, {"Allow": "POST, OPTIONS"})
                return
            self._send_empty(404)

        def do_POST(self):
            if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
                self._send_empty(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""  # always drain body, even on auth failure, or keep-alive desyncs

            if not self._authorized():
                self._send_json(401, {"error": "unauthorized"})
                return

            try:
                req = json.loads(raw or b"{}")
            except json.JSONDecodeError as e:
                self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": f"parse error: {e}"}})
                return

            resp, status = dispatch(req, state)
            if resp is None:
                self._send_empty(status)
                return
            self._send_json(status, resp)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    httpd = socketserver.ThreadingTCPServer((host, port), Handler)
    httpd.daemon_threads = True
    log(f"HTTP transport listening on http://{host}:{port}/mcp")
    log("auth: bearer token required" if auth_token
        else "auth: NONE - server is open to anyone who can reach it. Set MCP_AUTH_TOKEN to require a bearer token.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    argv = sys.argv[1:]
    skills_dir = resolve_skills_dir(argv)
    state = {"skills": discover(skills_dir), "initialized": False}
    log(f"skills dir: {skills_dir}")
    log(f"loaded {len(state['skills'])} skill(s): {', '.join(state['skills']) or '(none)'}")

    use_http = "--http" in argv
    if use_http:
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8787"))
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                port = int(argv[i + 1])
            elif a.startswith("--port="):
                port = int(a.split("=", 1)[1])
            elif a == "--host" and i + 1 < len(argv):
                host = argv[i + 1]
            elif a.startswith("--host="):
                host = a.split("=", 1)[1]
        auth_token = os.environ.get("MCP_AUTH_TOKEN")
        run_http_server(state, host, port, auth_token)
        return

    stdin = sys.stdin
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": f"parse error: {e}"}}) + "\n")
            sys.stdout.flush()
            continue

        resp, _status = dispatch(req, state)
        if resp is None:
            continue  # notification: no response

        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
