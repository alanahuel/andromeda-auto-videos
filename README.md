# Andromeda render-service

Servicio que ensambla anuncios de vídeo a partir de tres clips raw (Hook + Cuerpo + CTA) y una pista de música. Está pensado para ser disparado por Make.com como parte del pipeline de Andrómeda: Make envía los cuatro archivos vía `multipart/form-data` y recibe el MP4 ensamblado en la misma respuesta HTTP. **Síncrono y sin estado externo.**

## Arquitectura

```
            ┌──────────┐    POST /jobs (multipart)    ┌──────────────┐
  Airtable ─┤ Make.com ├──────────────────────────────►   FastAPI    │
            └──────────┘   X-API-Key + 4 archivos     │     (api)    │
                          ◄──────────────────────────  │   + ffmpeg   │
                            200 OK · video/mp4         └──────────────┘
```

Un solo contenedor: `api`. FFmpeg corre dentro del proceso. Las renders se serializan con un `asyncio.Semaphore(1)` (FFmpeg es CPU-bound y el VPS tiene 2 vCPU). No hay cola, ni Redis, ni callbacks, ni Drive.

## Estructura del repo

```
render-service/
├── api/                # FastAPI + ffmpeg pipeline
│   ├── src/
│   │   ├── main.py                 # endpoints
│   │   ├── auth.py                 # X-API-Key (hmac.compare_digest)
│   │   ├── render_orchestrator.py  # Semaphore + workdir + stream out
│   │   ├── ffmpeg_pipeline.py      # probe + concat + mix music
│   │   └── settings.py
│   ├── tests/
│   └── Dockerfile
├── shared/             # Pydantic models compartidos
├── docker-compose.yml
├── .env.example
└── DEPLOY.md
```

## Correrlo en local

### 1. Pre-requisitos

- Docker Engine 24+ y Docker Compose v2.
- (Opcional, primera vez) `uv` instalado para generar el lockfile:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Configurar `.env`

```bash
cp .env.example .env
# Edita .env y rellena RENDER_API_KEY
```

Genera una API key fuerte:

```bash
openssl rand -hex 32
```

Solo necesitas tres variables (todas las viejas de Drive/Redis/Worker desaparecieron):

| Variable                | Valor                                        |
|-------------------------|----------------------------------------------|
| `RENDER_API_KEY`        | string aleatorio largo                       |
| `ORIENTATION_STRATEGY`  | `crop` (default) o `pad`                     |
| `LOG_LEVEL`             | `INFO` (default)                             |

### 3. (Primera vez) Generar el lockfile

```bash
cd api && uv lock && cd ..
```

Si no tienes `uv` local, el Dockerfile cae a `uv sync --no-dev` (resuelve en caliente). Útil para arrancar pero menos reproducible — commitea el lockfile cuanto antes.

### 4. (Opcional) Override para dev con hot-reload

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Con el override, `api/src/` y `shared/` se bind-montan y la API recarga al editar.

### 5. Arrancar

```bash
docker compose up --build
```

Un solo contenedor arranca. Logs JSON estructurados en stdout.

### 6. Verificar

```bash
# Health (sin auth)
curl -s http://localhost:8000/health
# {"status":"ok"}

# Render real — necesitas 4 archivos en el directorio actual
curl -s -X POST http://localhost:8000/jobs \
  -H "X-API-Key: $RENDER_API_KEY" \
  -F "clip_hook=@hook.mp4" \
  -F "clip_cuerpo=@cuerpo.mp4" \
  -F "clip_cta=@cta.mp4" \
  -F "music=@music.mp3" \
  -F 'params={"orientation":"vertical","music_volume":0.3,"fade_in":2,"fade_out":2,"output_name":"smoke_test"}' \
  -o out.mp4 -D headers.txt
```

Si todo va bien: respuesta `200 OK` con `Content-Type: video/mp4` y los headers extra `X-Output-Duration-Seconds` y `X-Concat-Strategy: fast|reencode`. El MP4 cae en `out.mp4`.

## API

| Método | Path      | Auth        | Para                                      |
|--------|-----------|-------------|-------------------------------------------|
| GET    | `/health` | —           | UptimeRobot                               |
| POST   | `/jobs`   | `X-API-Key` | Make manda los 4 archivos y recibe el MP4 |

FastAPI sirve el OpenAPI auto-generado en `http://localhost:8000/docs`.

### Request — `POST /jobs`

Content-Type: `multipart/form-data`. Campos:

| Campo         | Tipo    | Notas                                                |
|---------------|---------|------------------------------------------------------|
| `clip_hook`   | file    | MP4                                                  |
| `clip_cuerpo` | file    | MP4                                                  |
| `clip_cta`    | file    | MP4                                                  |
| `music`       | file    | MP3 / M4A / WAV — libavformat sniff lo resuelve      |
| `params`      | string  | JSON serializado de `JobParams` (ver `shared/models.py`) |

Schema de `params`:

```json
{
  "orientation": "vertical",   // "vertical" | "horizontal"
  "music_volume": 0.3,         // 0.0 – 1.0
  "fade_in": 2.0,              // 0.0 – 10.0 segundos
  "fade_out": 2.0,             // 0.0 – 10.0 segundos
  "output_name": "ad_2026_05"  // ^[A-Za-z0-9._-]+$, max 200 chars
}
```

### Response — éxito

- Status: `200 OK`
- Content-Type: `video/mp4`
- `Content-Disposition: attachment; filename="<output_name>.mp4"`
- `X-Output-Duration-Seconds: <duración del vídeo final>`
- `X-Concat-Strategy: fast | reencode`
- Body: bytes del MP4.

### Response — error

- `401` si falta o no coincide el `X-API-Key`.
- `422` si `params` no parsea o no cumple el schema.
- `500` con body JSON `{"error": "<mensaje en español>"}` si el render falla. Los errores conocidos llevan el mensaje original (en español, seguro de mostrar a usuario). Los inesperados se ofuscan como `"Error inesperado en el render — revisa los logs del servicio."`.

## Logs

JSON estructurado en stdout. Cada línea incluye `job_id` y `output_name` cuando aplica.

```bash
docker compose logs -f api | jq -R 'fromjson? // .'
```

Eventos clave: `job_started`, `concat_fast_path` / `concat_reencode_path`, `job_done`, `job_failed`, `job_failed_unexpected`.

## Tests

Builders FFmpeg + integration test del endpoint (subprocess y `run_pipeline` mockeados — no se necesita ffmpeg para correrlos):

```bash
cd api
uv sync
uv run pytest -q
```

No hay tests E2E con clips reales en el repo.

## Decisiones que se tomaron fuera de la spec

- **Layout de `shared/`**: el módulo común se monta en `/app/shared` (build context = raíz del repo). Imports siguen siendo `from shared.models import ...`. El `conftest.py` del api añade la raíz al `sys.path` para dev local fuera de Docker.
- **Manejo de clips sin audio**: si alguno de los 3 clips no tiene stream de audio, el pipeline añade un input `lavfi anullsrc` con duración igual al clip y lo enchufa en el `concat=`. Robusto sin caer en "stream not found".
- **`afade=t=out` con `fade_out > duration`**: el `start` del fade se clampea a 0.
- **`output_name`**: regex `^[A-Za-z0-9._-]+$` — sin espacios ni separadores raros que rompan el shell.
- **`hmac.compare_digest`** en `require_api_key` — comparación constante en el tiempo.
- **`+faststart`** en todos los outputs MP4.
- **Output en memoria**: el MP4 final se carga entero en memoria antes de limpiar el workdir. OK mientras los renders sean < 200 MB; revisar si la cota sube.
- **`uvicorn --workers 1`**: con más workers cada uno tendría su propio Semaphore → races por la CPU.
- **`FFMPEG_TIMEOUT_SECONDS=600`** está hardcoded en `ffmpeg_pipeline.py`. Si necesitas tunear, edita la constante.

## Producción

Para deploy en Easypanel + Hetzner ver [DEPLOY.md](./DEPLOY.md).
