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
import json
import mimetypes
import os
import socketserver
import sys
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

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            if not auth_token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {auth_token}"

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/health"):
                self._send_json(200, {"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})
                return
            self.send_response(404)
            self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path.split("?", 1)[0].rstrip("/") != "/mcp":
                self.send_response(404)
                self._cors()
                self.end_headers()
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
                self.send_response(status)
                self._cors()
                self.end_headers()
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
