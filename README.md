# hr-talent-coordinator — servidor MCP

Sirve la skill **hr-talent-coordinator** por Model Context Protocol, para que cualquier
cliente compatible con MCP pueda descubrirla y cargarla.

Sin dependencias: solo biblioteca estándar de Python (3.8+). Transporte stdio.

---

## 1. Contenido

```
hr-talent-coordinator-mcp/
├── server.py                          servidor MCP (stdio y HTTP, JSON-RPC 2.0)
├── README.md
├── claude_desktop_config.example.json config para Claude Desktop (stdio)
├── mcp.example.json                   config por proyecto para Claude Code (stdio)
├── mcp.remote.example.json            config por proyecto para Claude Code (HTTP remoto)
├── Dockerfile                         imagen para desplegar el transporte HTTP
├── .dockerignore
└── skills/
    └── hr-talent-coordinator/
        ├── SKILL.md                   las instrucciones del agente
        └── assets/
            ├── HR_Coordinator_Toolkit.xlsx
            ├── Hiring_Intake_and_Screening_Kit.docx
            └── Offer_Letter_Template.docx
```

## 2. Instalación

1. Descomprime la carpeta donde quieras, por ejemplo `C:\Users\DELL\hr-talent-coordinator-mcp`.
2. Verifica que Python responde:

   ```
   python --version
   ```

   Si en tu sistema el comando es `py` o `python3`, usa ese nombre en la configuración.

3. Prueba el servidor a mano (opcional). Debe imprimir en stderr cuántas skills cargó
   y quedarse esperando; ciérralo con Ctrl+C:

   ```
   python C:\Users\DELL\hr-talent-coordinator-mcp\server.py
   ```

## 3. Registro en Claude Code

```
claude mcp add hr-talent-coordinator -- python C:\Users\DELL\hr-talent-coordinator-mcp\server.py
```

Para que quede versionado con un repositorio, copia `mcp.example.json` como `.mcp.json`
en la raíz del proyecto y ajusta la ruta.

## 4. Registro en Claude Desktop

Añade el bloque de `claude_desktop_config.example.json` a tu `claude_desktop_config.json`
(en Windows: `%APPDATA%\Claude\claude_desktop_config.json`), corrige la ruta y reinicia
la aplicación.

## 5. Despliegue remoto (HTTP)

El servidor también puede correr como servidor HTTP en vez de stdio, para que clientes
remotos (no solo procesos locales) se conecten por red. Usa el mismo `server.py`, sin
dependencias nuevas — el transporte HTTP está hecho con la biblioteca estándar
(`http.server`).

### 5.1 Arrancarlo en modo HTTP

```
python server.py --http --port 8787
```

Variables de entorno equivalentes a los flags: `PORT`, `HOST` (por defecto `0.0.0.0`).

**Importante — protégelo con un token.** Sin token, cualquiera que alcance el puerto
puede leer las skills. Genera uno y expórtalo antes de arrancar:

```
python -c "import secrets; print(secrets.token_urlsafe(32))"
export MCP_AUTH_TOKEN="el-token-generado"
python server.py --http --port 8787
```

Con `MCP_AUTH_TOKEN` puesto, cada request a `/mcp` debe traer
`Authorization: Bearer el-token-generado`; si no, responde `401`. `GET /health` queda
siempre abierto (sin datos sensibles) para que un balanceador o un healthcheck lo use
sin credenciales.

### 5.2 Probarlo a mano

```
curl http://127.0.0.1:8787/health

curl -X POST http://127.0.0.1:8787/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer el-token-generado" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### 5.3 Contenerizarlo

Ya incluye `Dockerfile`:

```
docker build -t hr-talent-coordinator-mcp .
docker run -p 8787:8787 -e MCP_AUTH_TOKEN="el-token-generado" hr-talent-coordinator-mcp
```

### 5.4 Dónde hospedarlo

Cualquier opción que corra un contenedor o un proceso Python de larga duración y exponga
un puerto sirve. Dos caminos típicos:

- **PaaS (Render, Railway, Fly.io, etc.)** — conecta el repo o sube la imagen Docker,
  define `MCP_AUTH_TOKEN` como variable de entorno/secreto, y deja que la plataforma
  maneje TLS y el dominio público. Es el camino con menos partes que mantener.
- **VPS propio** — clona el repo, corre el servidor como servicio (`systemd`), y pon un
  reverse proxy (nginx o Caddy) delante para TLS:

  ```ini
  # /etc/systemd/system/hr-talent-coordinator-mcp.service
  [Unit]
  Description=hr-talent-coordinator MCP server
  After=network.target

  [Service]
  Environment=MCP_AUTH_TOKEN=el-token-generado
  Environment=PORT=8787
  ExecStart=/usr/bin/python3 /opt/hr-talent-coordinator-mcp/server.py --http
  Restart=on-failure
  User=www-data

  [Install]
  WantedBy=multi-user.target
  ```

  Caddy resuelve TLS automáticamente con un solo `Caddyfile`:

  ```
  tu-dominio.example.com {
      reverse_proxy 127.0.0.1:8787
  }
  ```

  Abre solo el puerto 443 (o 80/443) en el firewall; el 8787 debe quedar solo accesible
  desde `localhost`, detrás del proxy.

### 5.5 Registrar el servidor remoto en un cliente

Con Claude Code:

```
claude mcp add --transport http hr-talent-coordinator https://tu-dominio.example.com/mcp \
  --header "Authorization: Bearer el-token-generado"
```

O copiando `mcp.remote.example.json` como `.mcp.json` y ajustando `url` y el token.

## 6. Verificación

Con `/mcp` confirma que el servidor aparece conectado. Luego:

- `list_skills` debe devolver `hr-talent-coordinator`.
- `get_skill` con `name: hr-talent-coordinator` debe devolver las instrucciones completas
  y la ruta absoluta del directorio de la skill.

## 7. Herramientas expuestas

| Herramienta | Qué hace |
|---|---|
| `list_skills` | Lista las skills disponibles con nombre, descripción y ruta absoluta. |
| `get_skill` | Devuelve el `SKILL.md` completo, la ruta del directorio y los archivos incluidos. |
| `list_skill_files` | Lista los archivos de la skill con ruta relativa, ruta absoluta y tamaño. |
| `read_skill_file` | Lee un archivo incluido. Texto en UTF-8; binarios (xlsx, docx, pdf) en base64. |

Además expone:

- **Prompts** — la skill aparece como prompt invocable, con un argumento opcional `task`.
  En clientes que soportan prompts funciona como comando de barra.
- **Resources** — cada archivo incluido está disponible en `skill://hr-talent-coordinator/<ruta>`.

## 8. Añadir más skills

Crea otra carpeta dentro de `skills/` con su propio `SKILL.md`. El servidor las descubre
al arrancar; reinicia el cliente después de añadirla.

Para apuntar a un directorio de skills distinto:

```
python server.py --skills-dir C:\ruta\a\skills
```

También se respeta la variable de entorno `SKILLS_DIR`.

## 9. Notas

- El servidor solo lee archivos; no escribe nada.
- Las rutas se validan contra el directorio de la skill: un `path` que intente salir de
  ella se rechaza.
- Los diagnósticos van a stderr; stdout queda reservado para el protocolo.
- Las plantillas son andamiaje operativo, no asesoría legal. Los plazos, versiones de
  formularios y umbrales deben verificarse contra la norma vigente antes de usarlos.
