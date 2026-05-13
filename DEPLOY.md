# Deploy en Easypanel (Hetzner Ubuntu 24.04)

Pasos exactos para llevar `render-service` a producción en el VPS de Andrómeda. El servicio quedará expuesto en `https://render.charlysway.com` con HTTPS automático (Traefik + Let's Encrypt que ya gestiona Easypanel).

## 0. Pre-requisitos

- Acceso de admin al panel de Easypanel.
- Un repo Git con esta carpeta `render-service/` en la raíz **o** como subdirectorio. Si es subdirectorio, anota el `Build path` para el paso 2.
- DNS de `render.charlysway.com` apuntando al VPS (A record). Si todavía no, créalo antes de continuar; Let's Encrypt lo necesita.

> El servicio ya no usa Google Drive ni Redis. No hace falta Service Account ni base de datos externa.

## 1. Crear el proyecto y el servicio en Easypanel

1. Crea un proyecto llamado `andromeda` (o el que prefieras).
2. Dentro del proyecto, crea **un solo servicio** tipo "App" llamado `api`.

### Servicio `api`

- Source: **Git**, URL del repo, branch `master`.
- Build path: la ruta hasta `render-service/` dentro del repo (vacío si está en raíz).
- Build command: **vacío** — el `api/Dockerfile` se encarga.
- Dockerfile path: `api/Dockerfile`.
- Build context: la raíz `render-service/` (esto es lo que asume el Dockerfile: necesita ver `shared/` además de `api/`).
- Puerto interno: 8000.
- Domains: añade `render.charlysway.com` apuntando al puerto 8000. Easypanel pedirá automáticamente el cert Let's Encrypt.
- Recursos: 2 vCPU y al menos 4 GiB de memoria (FFmpeg + buffer del MP4 en memoria). En el `docker-compose.yml` se declara `mem_limit: 6g`; Easypanel respeta ese límite si lo importas.
- Restart policy: `unless-stopped`.

> **No subir `--workers` por encima de 1.** Las renders se serializan con un `asyncio.Semaphore(1)` por proceso. Más workers = races por la CPU y OOM. El `Dockerfile` ya fija `--workers 1`.

## 2. Variables de entorno

Configura solo tres variables en el servicio `api`. **Cualquier env var del flujo antiguo (`REDIS_URL`, `GOOGLE_SERVICE_ACCOUNT_*`, `WORKER_*`, `FFMPEG_TIMEOUT_SECONDS`, `JOB_TIMEOUT_SECONDS`, `WORKDIR_BASE`) debe quedar borrada.**

| Variable                | Valor                                                       |
|-------------------------|-------------------------------------------------------------|
| `RENDER_API_KEY`        | `openssl rand -hex 32` — el mismo string que use Make.com   |
| `ORIENTATION_STRATEGY`  | `crop` (default) o `pad`                                    |
| `LOG_LEVEL`             | `INFO`                                                      |

Easypanel encripta las variables de entorno y no las muestra en logs. La `RENDER_API_KEY` también va guardada en Make.com como header del HTTP module que llama a `/jobs`.

## 3. Apuntar el dominio

`render.charlysway.com` ya debe tener un A record al IP del VPS.

En Easypanel → servicio `api` → "Domains":

- Host: `render.charlysway.com`
- Port: `8000`
- HTTPS: activado (Let's Encrypt). Espera ~30s a que emita el certificado.

Si el cert no se emite a la primera, revisa que el DNS esté propagado (`dig render.charlysway.com +short`).

## 4. Deploy

Pulsa **Deploy** en el servicio. Easypanel construye la imagen y arranca el contenedor.

Tiempo aproximado: 3–4 minutos (instala `ffmpeg` con apt + uv sync). Builds posteriores cachean ambos pasos.

## 5. Verificar que funciona

### a) Health endpoint

```bash
curl -s https://render.charlysway.com/health
# {"status":"ok"}
```

Si devuelve 5xx, revisa los logs del contenedor.

### b) FFmpeg dentro del contenedor

En Easypanel, abre el "Terminal" del servicio `api`:

```bash
ffmpeg -version | head -1
# ffmpeg version 6.x ...
```

### c) Auth

```bash
# Sin key → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://render.charlysway.com/jobs
# 401

# Con key + sin campos → 422
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://render.charlysway.com/jobs \
  -H "X-API-Key: <tu_key>"
# 422
```

### d) Render real

Manda el primer job real desde un Make scenario de prueba o con `curl`:

```bash
curl -s -X POST https://render.charlysway.com/jobs \
  -H "X-API-Key: $RENDER_API_KEY" \
  -F "clip_hook=@hook.mp4" \
  -F "clip_cuerpo=@cuerpo.mp4" \
  -F "clip_cta=@cta.mp4" \
  -F "music=@music.mp3" \
  -F 'params={"orientation":"vertical","music_volume":0.3,"fade_in":2,"fade_out":2,"output_name":"smoke_test"}' \
  -o out.mp4 -D headers.txt
```

Confirma:

- `headers.txt` muestra `200 OK`, `content-type: video/mp4`, `x-concat-strategy: fast|reencode` y `x-output-duration-seconds`.
- `out.mp4` se reproduce localmente.
- En los logs del `api` aparece `job_started` → `job_done` con `duration_seconds` y `elapsed_seconds`.

### e) UptimeRobot

Añade un monitor HTTPS a `https://render.charlysway.com/health` con expected status 200 y palabra clave `"ok"`. Alerta a tu canal de Slack si baja.

## 6. Operación

### Logs

En Easypanel cada servicio tiene tab "Logs". Para tail tipo `tail -f`, usa el terminal:

```bash
docker logs -f <container_name> 2>&1 | tail -200
```

Los eventos clave a buscar: `job_started`, `job_done` (éxito), `job_failed` (error con mensaje en español), `job_failed_unexpected` (error inesperado, mensaje genérico al cliente — el detalle queda en el log).

### Reinicios

Hacer "Restart" del `api` cancela cualquier render en curso (el cliente recibirá un error de conexión). Como el servicio es síncrono, no hay nada que limpiar en Redis ni en disco — los workdirs viven en `/tmp/render_*` y desaparecen con el contenedor.

### Actualizar el código

Push a `master` → en Easypanel pulsa "Deploy". Easypanel rebuildea y rota el contenedor. Como cada request es independiente, no hay ventana de incompatibilidad de schema entre versiones.

### Disco

Los workdirs viven en `/tmp/render_<uuid>_*/` y se limpian siempre en `finally`. Si por algún bug se acumulan, en el terminal del contenedor:

```bash
ls /tmp | head
# Si ves muchos render_* viejos, algo va mal: rm -rf /tmp/render_<uuid>_*
```

### Memoria

El MP4 final se carga entero en memoria antes de limpiar el workdir y antes de enviar la respuesta. Con `mem_limit: 6g` el margen aguanta renders hasta ~1 GB sin sudar. Si los renders crecen, hay que cambiar a streaming desde disco (ver nota en `render_orchestrator.py`).

## Checklist final tras deploy

- [ ] `/health` responde 200 desde el dominio público.
- [ ] `/jobs` rechaza 401 sin API key.
- [ ] Render real procesado end-to-end: 4 archivos in → MP4 out con headers correctos.
- [ ] UptimeRobot configurado y enviando heartbeats a tu Slack.
- [ ] `RENDER_API_KEY` guardada en el password manager y en Make.com.
- [ ] Sin secrets en el repo (`git log -p` no muestra la key).
- [ ] **Make.com migrado al nuevo flujo multipart** — el endpoint ya no acepta JSON ni emite callbacks.
