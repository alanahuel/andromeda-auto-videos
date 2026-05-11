# Deploy en Easypanel (Hetzner Ubuntu 24.04)

Pasos exactos para llevar `render-service` a producción en el VPS de Andrómeda. El servicio quedará expuesto en `https://render.charlysway.com` con HTTPS automático (Traefik + Let's Encrypt que ya gestiona Easypanel).

## 0. Pre-requisitos

- Acceso de admin al panel de Easypanel.
- Un repo Git (GitHub privado recomendado) con esta carpeta `render-service/` en la raíz **o** como subdirectorio. Si es subdirectorio, anota el `Build path` para el paso 2.
- DNS de `render.charlysway.com` apuntando al VPS (A record). Si todavía no, créalo antes de continuar; Let's Encrypt lo necesita.
- El archivo JSON de la Service Account de Google Cloud con scope `https://www.googleapis.com/auth/drive`, y permisos de Editor sobre todas las carpetas de Drive involucradas (clips raw + outputs).

## 1. Crear el proyecto y los servicios en Easypanel

1. Crea un proyecto llamado `andromeda` (o el que prefieras).
2. Dentro del proyecto, crea **tres servicios**:
   - `redis` (tipo "Redis" del catálogo de Easypanel) o, si quieres mantener todo igual al `docker-compose.yml`, despliega `redis:7-alpine` como servicio "App". Recomendado: usar el servicio "Redis" del catálogo, gestionado.
   - `api` (servicio "App", desde Git)
   - `worker` (servicio "App", desde Git)

### Servicio Redis

Si usas el del catálogo, anota la URL interna que muestra Easypanel — típicamente `redis://andromeda_redis:6379`. Esta es la `REDIS_URL` para los otros dos servicios.

Si despliegas `redis:7-alpine` como App, expón el puerto 6379 solo en la red interna del proyecto (no público), con persistencia en un volumen para no perder estado de RQ en restart.

### Servicio `api`

- Source: **Git**, URL del repo, branch `main`.
- Build path: la ruta hasta `render-service/` dentro del repo (vacío si está en raíz).
- Build command: **vacío** — el `api/Dockerfile` se encarga.
- Dockerfile path: `api/Dockerfile`.
- Build context: la raíz `render-service/` (esto es lo que asume el Dockerfile: necesita ver `shared/` además de `api/`).
- Puerto interno: 8000.
- Domains: añade `render.charlysway.com` apuntando al puerto 8000. Easypanel pedirá automáticamente el cert Let's Encrypt.

### Servicio `worker`

- Source: **Git**, mismo repo y rama.
- Build path: igual que el de `api`.
- Dockerfile path: `worker/Dockerfile`.
- Build context: raíz `render-service/`.
- **Sin dominio expuesto** — el worker no acepta tráfico HTTP.
- Recursos: 2 vCPU y al menos 2 GiB de memoria. Easypanel respeta los `mem_limit` del compose si lo importas; si lo configuras manualmente, pon ~3 GiB de límite.
- Restart policy: `unless-stopped`.

## 2. Variables de entorno

Configura ambas variables en `api` **y** `worker`. La API solo necesita las 3 primeras; el worker necesita todas.

| Variable                              | Valor                                        | Quien la usa  |
|---------------------------------------|----------------------------------------------|---------------|
| `RENDER_API_KEY`                      | `openssl rand -hex 32` — el mismo Make.com   | api           |
| `REDIS_URL`                           | `redis://andromeda_redis:6379/0` (o lo que muestre Easypanel) | api, worker   |
| `LOG_LEVEL`                           | `INFO`                                       | api, worker   |
| `JOB_TIMEOUT_SECONDS`                 | `600`                                        | api, worker   |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64`     | (ver siguiente sección)                      | worker        |
| `FFMPEG_TIMEOUT_SECONDS`              | `600`                                        | worker        |
| `ORIENTATION_STRATEGY`                | `crop` (o `pad` si prefieres letterbox)      | worker        |
| `WORKDIR_BASE`                        | `/tmp`                                       | worker        |
| `WORKER_CONCURRENCY`                  | `1`                                          | worker        |

Easypanel encripta las variables de entorno y no las muestra en logs. La `RENDER_API_KEY` también va guardada en Make.com como parte de los headers del HTTP module que llama a `/jobs`.

## 3. Subir el JSON de la Service Account

Tienes dos opciones; elige la que prefieras.

### Opción A (recomendada para Easypanel): base64 en una env var

1. En tu máquina:
   ```bash
   cat sa.json | base64 -w 0
   ```
2. Copia la cadena resultante.
3. En Easypanel, añade la env var `GOOGLE_SERVICE_ACCOUNT_JSON_B64` al servicio `worker` y pega el valor.
4. Deja `GOOGLE_SERVICE_ACCOUNT_JSON` vacío.

Ventaja: una sola env var, no hace falta volumen.

### Opción B: volumen montado con el JSON

1. En Easypanel, edita el servicio `worker`, sección "Mounts".
2. Crea un mount tipo "File" con contenido pegado del JSON, montado en `/secrets/sa.json` modo solo lectura.
3. En env vars del worker, define `GOOGLE_SERVICE_ACCOUNT_JSON=/secrets/sa.json` y deja `GOOGLE_SERVICE_ACCOUNT_JSON_B64` vacío.

Si configuras AMBAS, el código prioriza la opción A solo si la opción B está vacía — define solo una.

## 4. Apuntar el dominio

`render.charlysway.com` ya debe tener un A record al IP del VPS.

En Easypanel → servicio `api` → "Domains":

- Host: `render.charlysway.com`
- Port: `8000`
- HTTPS: activado (Let's Encrypt). Espera ~30s a que emita el certificado.

Si el cert no se emite a la primera, revisa que el DNS esté propagado (`dig render.charlysway.com +short`).

## 5. Deploy

Pulsa **Deploy** en cada uno de los tres servicios (orden recomendado: `redis` → `worker` → `api`). Easypanel construye las imágenes y arranca los contenedores.

Tiempo aproximado: 2–3 minutos por servicio (el worker tarda más porque instala `ffmpeg` con apt).

## 6. Verificar que funciona

### a) Health endpoint

```bash
curl -s https://render.charlysway.com/health
# {"status": "ok", "redis": "ok"}
```

Si devuelve 503 con `redis: down`, revisa `REDIS_URL` en ambos servicios.

### b) FFmpeg en el worker

En Easypanel, abre el "Terminal" del servicio `worker`:

```bash
ffmpeg -version | head -1
# ffmpeg version 6.x ...
```

Si falla, el build no debería haber pasado (el Dockerfile fuerza `ffmpeg -version` al final).

### c) API key

```bash
# Sin key → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://render.charlysway.com/jobs \
  -H "Content-Type: application/json" -d '{}'
# 401

# Con key + body inválido → 422
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://render.charlysway.com/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <tu_key>" \
  -d '{}'
# 422
```

### d) Job real

Manda el primer job real desde un Make scenario de prueba o con `curl`. Confirma en los logs del `worker`:

- `job_started` aparece al recoger el job.
- `downloading_clips` y `uploading_output` aparecen sin errores.
- `job_done` con `duration_seconds`.
- Llega el callback a Make (Make lo registra como ejecución).

### e) UptimeRobot

Añade un monitor HTTPS a `https://render.charlysway.com/health` con expected status 200 y palabra clave `"ok"`. Alerta a tu canal de Slack si baja a 503 o no responde.

## 7. Operación

### Logs

En Easypanel cada servicio tiene tab "Logs". Para tail tipo `tail -f`, usa el terminal:

```bash
docker logs -f <container_name> 2>&1 | tail -200
```

### Cargas en cola

```bash
# Desde el terminal del servicio api o worker
redis-cli -u $REDIS_URL llen "rq:queue:renders"
```

### Reinicios

Hacer "Restart" del worker es seguro mientras no haya un job en curso. Si lo haces durante un job, ese job se marca como failed (interrupted) y RQ lo deja en la dead-letter; el callback se envía como `failed`.

### Actualizar el código

Push a `main` → en Easypanel cada servicio tiene "Deploy" que rebuildea. Recomendado: deploy primero del worker, luego de la api, para minimizar la ventana en que la api enquea jobs que el worker viejo no puede procesar (irrelevante salvo refactors del job_runner).

### Disco

Los workdirs viven en `/tmp/{job_id}/` y se limpian siempre en `finally`. Si por algún bug se acumulan, en Easypanel terminal:

```bash
ls /tmp | head
# Si ves muchos UUIDs, algo va mal: rm -rf /tmp/{job_id} de los más viejos.
```

## Checklist final tras deploy

- [ ] `/health` responde 200 desde el dominio público.
- [ ] `/jobs` rechaza 401 sin API key.
- [ ] Job real procesado end-to-end: descarga → ffmpeg → upload → callback.
- [ ] UptimeRobot configurado y enviando heartbeats a tu Slack.
- [ ] La Service Account tiene acceso a las carpetas de Drive de producción.
- [ ] `RENDER_API_KEY` guardada en el password manager y en Make.com.
- [ ] Sin secrets en el repo (`git log -p` no muestra `sa.json` ni la key).
