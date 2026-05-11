# Andromeda render-service

Servicio que ensambla anuncios de vídeo a partir de tres clips raw (Hook + Cuerpo + CTA) y una pista de música. Está pensado para ser disparado por Make.com como parte del pipeline de Andrómeda: Make manda un POST con los IDs de Drive, el servicio descarga, concatena con FFmpeg, mezcla música, sube a Drive, y devuelve el resultado vía callback.

## Arquitectura

```
            ┌─────────┐      POST /jobs       ┌────────────┐
  Airtable ─┤ Make.com ├──────────────────────► FastAPI    │
            └─────────┘   X-API-Key + JSON    │  (api)     │
                                              └─────┬──────┘
                                                    │ enqueue
                                              ┌─────▼──────┐
                                              │   Redis    │
                                              │   (RQ)     │
                                              └─────┬──────┘
                                                    │
                                              ┌─────▼──────┐
                                              │  worker    │  ffmpeg
                                              │            ├─────────► Google Drive
                                              └─────┬──────┘
                                                    │ POST callback_url
                                                    ▼
                                                 Make.com
```

Tres contenedores: `api`, `worker`, `redis`. La API solo encola; el worker hace todo el trabajo (descarga → ffprobe → ffmpeg → upload → callback). Un solo job en paralelo por defecto.

## Estructura del repo

```
render-service/
├── api/                # FastAPI front door
├── worker/             # RQ worker + ffmpeg + Drive
├── shared/             # Pydantic models compartidos (contrato del wire)
├── docker-compose.yml
├── .env.example
└── DEPLOY.md           # pasos en Easypanel
```

## Correrlo en local

### 1. Pre-requisitos

- Docker Engine 24+ y Docker Compose v2.
- (Opcional, primera vez) `uv` instalado para generar lockfiles:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

### 2. Configurar `.env`

```bash
cp .env.example .env
# Edita .env y rellena RENDER_API_KEY + Service Account
```

Genera una API key fuerte:

```bash
openssl rand -hex 32
```

Para la Service Account de Google Drive, tienes dos opciones (elige UNA):

- **`GOOGLE_SERVICE_ACCOUNT_JSON`** — ruta al archivo dentro del contenedor (típico con bind-mount). En `docker-compose.override.yml.example` ya hay un ejemplo: deja tu `sa.json` en la raíz del repo y el worker lo verá en `/secrets/sa.json`.
- **`GOOGLE_SERVICE_ACCOUNT_JSON_B64`** — el JSON entero codificado en base64 (típico en Easypanel). Genérala con:
  ```bash
  cat sa.json | base64 -w 0
  ```

Importante: la cuenta de servicio necesita acceso de **Editor** a los clips raw y la carpeta de output. Comparte cada carpeta de Drive con el email de la SA (`xxxx@xxxx.iam.gserviceaccount.com`).

### 3. (Primera vez) Generar lockfiles

Los `uv.lock` no están commiteados aún. Genéralos antes del primer build:

```bash
cd api && uv lock && cd ../worker && uv lock && cd ..
```

> Si no tienes `uv` local, el Dockerfile cae a `uv sync --no-dev` (resuelve en caliente). Útil para arrancar pero menos reproducible — commitea los lockfiles cuanto antes.

### 4. (Opcional) Override para dev

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Con el override, los `src/` se bind-montan y la API recarga al editar.

### 5. Arrancar

```bash
docker compose up --build
```

Tres contenedores arrancan. Logs JSON estructurados en stdout.

### 6. Verificar

```bash
# Health
curl -s http://localhost:8000/health | jq
# {"status": "ok", "redis": "ok"}

# FFmpeg dentro del worker
docker compose exec worker ffmpeg -version | head -1

# Job de prueba (falla en descarga porque los IDs no existen, pero confirma el flujo de validación + encolado)
curl -s -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RENDER_API_KEY" \
  -d '{
    "clips": [
      {"drive_id": "fake_hook_id", "role": "hook"},
      {"drive_id": "fake_cuerpo_id", "role": "cuerpo"},
      {"drive_id": "fake_cta_id", "role": "cta"}
    ],
    "music": {"drive_id": "fake_music_id", "volume": 0.3, "fade_in": 2.0, "fade_out": 2.0},
    "output": {"name": "smoke_test", "folder_drive_id": "fake_folder_id", "orientation": "vertical"},
    "callback_url": "https://httpbin.org/post",
    "metadata": {"airtable_record_id": "recXXX"}
  }' | jq
```

Si el body es válido y la API key correcta: respuesta `202 { "job_id": "...", "status": "queued" }`.

Para consultar:

```bash
curl -s -H "X-API-Key: $RENDER_API_KEY" http://localhost:8000/jobs/<job_id> | jq
```

## API

| Método | Path             | Auth          | Para                         |
|-------|------------------|---------------|------------------------------|
| GET   | `/health`        | —             | UptimeRobot                  |
| POST  | `/jobs`          | `X-API-Key`   | Make encola un render        |
| GET   | `/jobs/{job_id}` | `X-API-Key`   | Consulta de estado           |

El schema completo del request/response está en `shared/models.py` (Pydantic v2). FastAPI sirve el OpenAPI auto-generado en `http://localhost:8000/docs`.

### Callback hacia Make

El worker hace POST al `callback_url` del request con:

```json
{
  "job_id": "uuid",
  "status": "done|failed",
  "output_drive_id": "1xyz...",
  "output_url": "https://drive.google.com/file/d/1xyz/view",
  "duration_seconds": 87.4,
  "error": null,
  "metadata": { "...passthrough..." }
}
```

Retry: 3 intentos con backoff exponencial (5s, 15s, 45s). Si los 3 fallan, el job queda igualmente marcado `done` en Redis (consultable vía `GET /jobs/{id}`).

## Logs

JSON estructurado en stdout. Cada línea incluye `job_id` cuando aplica.

```bash
docker compose logs -f worker | jq -R 'fromjson? // .'
```

Eventos clave: `job_enqueued`, `job_started`, `downloading_clips`, `concat_fast_path` / `concat_reencode_path`, `uploading_output`, `job_done`, `job_failed`.

## Tests

Tests unitarios del builder de comandos FFmpeg (subprocess mockeado):

```bash
cd worker
uv sync
uv run pytest -q
```

No hay tests E2E en el repo — requieren clips reales y credenciales de Drive.

## Decisiones que se tomaron fuera de la spec

- **Layout de `shared/`**: el módulo común se monta en `/app/shared` dentro de ambas imágenes (Docker build context es la raíz del repo). Las imports son `from shared.models import ...` desde api y worker. Esto evita duplicar el archivo. Para dev local fuera de Docker, los `conftest.py` añaden la raíz al `sys.path`.
- **Manejo de clips sin audio**: si alguno de los 3 clips no tiene stream de audio, el worker añade un input `lavfi anullsrc` con duración igual al clip y lo enchufa en el `concat=`. El audio finalmente se mezcla con la música igual que en el caso normal. Esto es robusto sin caer al fallo "stream not found".
- **`afade=t=out` con `fade_out > duration`**: el `start` del fade se clampea a 0 en vez de devolver un valor negativo a FFmpeg.
- **`output.name`**: se valida con regex `^[A-Za-z0-9._-]+$` — sin espacios ni separadores raros que puedan romper Drive o el shell. Si el equipo audiovisual mete un guion bajo y un número, perfecto.
- **`hmac.compare_digest`** en `require_api_key` — comparación constante en el tiempo, no leak de longitud.
- **`MediaFileUpload(resumable=True)`** para subidas grandes, chunks de 8 MiB.
- **`+faststart`** en todos los outputs MP4 — Drive y reproductores web hacen seek inmediato.

## Producción

Para deploy en Easypanel + Hetzner ver [DEPLOY.md](./DEPLOY.md).
