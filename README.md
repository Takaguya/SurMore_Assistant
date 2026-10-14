# SurMore Assistant

Upload documents into knowledge bases, read and correct their text as Markdown, then chat with an NVIDIA-hosted LLM that answers from them (RAG) and cites the passages it used.

- **Knowledge bases**: create as many as you like; each holds its own files.
- **Files**: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xlsm), CSV, TXT, Markdown, and images (PNG, JPG, WebP, TIFF, BMP).
- **OCR**: scanned PDF pages, picture-only slides and images are read with NVIDIA Nemotron Parse, which returns Markdown (headings, lists, tables).
- **Document view**: every file is shown as Markdown, page by page (or slide / heading / block of rows). Edit any part to fix OCR mistakes or add context; the edit is re-indexed and searchable as soon as you save. PDF pages and images can be OCR'd again individually.
- **Chat**: pick one or more knowledge bases per chat; answers stream in with numbered citations. Each source opens the passage, the original file (PDFs at the right page) or the section in the document view.

```
backend/           FastAPI · PostgreSQL + pgvector · NVIDIA API (chat, embeddings, OCR)
frontend/          React + Vite + TypeScript
docker-compose.yml Local PostgreSQL + pgvector
```

## 1. Database (free)

Everything except the original uploaded files lives in PostgreSQL with the [pgvector](https://github.com/pgvector/pgvector) extension: knowledge bases, the Markdown of each document, the embeddings and the chats. Pick one:

**Local with Docker (free, no limits).** Needs [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```powershell
docker compose up -d     # in the project folder; Postgres listens on localhost:5433
```

The default `DATABASE_URL` in `backend/.env` already points at it. Data survives restarts (Docker volume `surmore-pgdata`).

**Hosted on Supabase (free tier).** Useful when the backend runs somewhere other than your PC.

1. Create a project at https://supabase.com.
2. Project Settings → Database → Connection string → **Session pooler**, copy the URI.
3. Put it in `backend/.env`: `DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-....pooler.supabase.com:5432/postgres`

The free tier gives a 500 MB database and pauses after a week without activity. Embeddings take most of the space; set `NVIDIA_EMBEDDING_DIMENSIONS=1024` (or 512) to fit more if your embedding model supports it. Neon or any other Postgres with pgvector 0.7+ works the same way.

The app creates its tables on startup. Original files are kept on the backend's disk in `backend/data/uploads/`.

## 2. Configure NVIDIA

Edit `backend/.env`:

```env
NVIDIA_API_KEY=nvapi-...                                   # from https://build.nvidia.com
NVIDIA_LLM_MODEL=meta/llama-3.3-70b-instruct               # any chat model on build.nvidia.com
NVIDIA_EMBEDDING_MODEL=nvidia/llama-3.2-nv-embedqa-1b-v2   # embedding model used for search
NVIDIA_OCR_MODEL=nvidia/nemotron-parse                     # OCR model (page image -> Markdown)
OCR_MODE=auto                                              # auto | always | off
```

`OCR_MODE=auto` only OCRs what has no readable text (scanned pages, picture-only slides, images), so a normal PDF costs no OCR calls. `always` sends every PDF page through OCR for the best Markdown structure, at one API call per page.

`backend/.env.example` documents every other setting. Restart the backend after changing `.env`.

> If you change `NVIDIA_EMBEDDING_MODEL` or `NVIDIA_EMBEDDING_DIMENSIONS` after uploading files, those files show **Needs re-indexing**. Click **Re-index**; your edits are kept.

## 3. Run the backend

```powershell
cd backend
python -m venv .venv              # first time only
.venv\Scripts\activate
pip install -r requirements.txt   # first time only
uvicorn app.main:app --reload
```

API runs on http://127.0.0.1:8000 (interactive docs at http://127.0.0.1:8000/docs).

## 4. Run the frontend

```powershell
cd frontend
npm install       # first time only
npm run dev
```

Open http://localhost:5180. Vite forwards `/api` to the backend.

**Single-process mode:** `npm run build` in `frontend/` produces `frontend/dist`, which the backend serves automatically, so http://127.0.0.1:8000 runs the whole app.

## Host it publicly

Locally the app runs without sign-in (`AUTH_MODE=none`). For a public site, turn on accounts so every visitor
only sees their own knowledge bases, files and chats, and put limits on how much of your NVIDIA quota each
account can use. With `ENVIRONMENT=production` the server refuses to start until sign-in is configured.

A free setup:

| Piece | Free option | What it holds |
|---|---|---|
| Database + sign-in | [Supabase](https://supabase.com) free plan | Postgres + pgvector (Markdown, embeddings, chats) and user accounts |
| Original files | [Cloudflare R2](https://developers.cloudflare.com/r2/) (10 GB free) | Uploaded PDFs, slides, spreadsheets |
| App server | [Render](https://render.com) free web service, or any host that runs a Dockerfile | The API and the built frontend, on one URL |

### 1. Supabase (database and sign-in)

1. Create a project. Under **Connect**, copy the **Session pooler** connection string; that's `DATABASE_URL`.
   The app creates its tables and the `vector` extension on first start.
2. **Project Settings > API Keys**: copy the project URL (`SUPABASE_URL`) and the `anon` / publishable key (`SUPABASE_ANON_KEY`).
3. **Authentication > URL Configuration**: set **Site URL** to your public URL (e.g. `https://surmore-assistant.onrender.com`)
   and add it under **Redirect URLs**.
4. **Authentication > Providers**: email is on by default. For Google, enable it and paste a Google OAuth client ID and
   secret. List the providers you enabled in `AUTH_PROVIDERS`, e.g. `email,google`.

### 2. Cloudflare R2 (original files)

Create a bucket, then an API token with **Object Read & Write** for it. Set `STORAGE_BACKEND=s3`, `S3_BUCKET`,
`S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
Any S3-compatible store works the same way (Supabase Storage's S3 endpoint, AWS S3, MinIO).

Skip this only if your host keeps files on a persistent disk (a Fly.io or Railway volume): then keep
`STORAGE_BACKEND=local` and point `DATA_DIR` at the volume. Render's free disk is wiped on every deploy.

### 3. Deploy

The `Dockerfile` builds the frontend and serves it from the API, so the whole app is one container on one URL.

- **Render**: push this project to GitHub, then **New > Blueprint** and pick the repository. `render.yaml` sets up
  the service; Render asks for each secret (database URL, Supabase, NVIDIA, R2).
- **Anywhere else** (Railway, Fly.io, Google Cloud Run, a VPS): build the `Dockerfile` and set the same environment
  variables. The container listens on `$PORT` (default 8000).

To try the production image on your machine:

```powershell
docker build -t surmore-assistant .
docker run -p 8000:8000 --env-file backend/.env surmore-assistant
```

### What's protected

- **Accounts**: every request carries the Supabase access token, which the API verifies (signature, expiry, issuer)
  before touching data. All queries are scoped to the signed-in user; other users' ids behave as if they don't exist.
- **Limits** (in `.env`, `0` = unlimited): messages per minute and per day, OCR re-runs per hour, uploads per day,
  knowledge bases, files and storage per account, files per upload. Rate limits are in memory, so they reset on
  restart and assume a single server process (the Dockerfile runs one).
- **Production mode**: API docs hidden, HTTPS-only (HSTS) and other security headers, and a content security policy
  on the web page.

Data created while sign-in was off belongs to the `local` user and doesn't show up for signed-in accounts.

## How it works

1. **Upload**: the file is saved to `backend/data/uploads/` (or the S3 bucket) and processed in the background; the UI shows progress.
2. **Convert to Markdown**: one section per PDF page, Word heading, slide (including speaker notes) or block of spreadsheet rows. Pages and slides without readable text, and image files, go through OCR.
3. **Store**: sections are saved in Postgres; that's what the document view shows and edits.
4. **Chunk + embed**: sections are split into ~1200-character overlapping chunks, embedded with the NVIDIA model (`input_type=passage`) and stored as pgvector `halfvec`.
5. **Ask**: the question is embedded (`input_type=query`); pgvector finds the closest chunks in the selected knowledge bases, and they go to the LLM with instructions to cite them as `[1]`, `[2]`…
6. **Stream**: the answer streams back over Server-Sent Events along with the sources.
7. **Edit**: saving a section, or re-running OCR on it, re-chunks and re-embeds only that section.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/health` | Which settings are configured |
| GET | `/api/public-config` | Sign-in settings the browser needs (public) |
| GET | `/api/me` | The signed-in user |
| GET/POST | `/api/knowledge-bases` | List / create knowledge bases |
| GET/PATCH/DELETE | `/api/knowledge-bases/{id}` | Read / rename / delete (with its files) |
| GET/POST | `/api/knowledge-bases/{id}/documents` | List / upload (multipart `files`) |
| GET/DELETE | `/api/documents/{id}` | Read / remove a file |
| POST | `/api/documents/{id}/reprocess?mode=auto\|extract\|reindex` | Retry, read the file again (replaces edits), or re-index (keeps edits) |
| GET | `/api/documents/{id}/file` | Original file |
| GET | `/api/documents/{id}/sections` | The document's Markdown sections |
| PUT | `/api/sections/{id}` | Save edited Markdown (`{"markdown": "..."}`) and re-index it |
| POST | `/api/sections/{id}/ocr` | Run OCR again on that PDF page or image |
| GET/POST | `/api/conversations` | List / create chats |
| GET/PATCH/DELETE | `/api/conversations/{id}` | Read (with messages) / update / delete |
| POST | `/api/conversations/{id}/messages` | Ask; SSE events `sources`, `token`, `done`, `error` |

## Limits

- Old binary formats (.doc, .ppt, .xls) aren't supported; save them as .docx/.pptx/.xlsx.
- Search is exact (no approximate index), which is accurate and fast up to tens of thousands of chunks per knowledge base. For far more, add an HNSW index on `chunks.embedding`.
- Without sign-in (`AUTH_MODE=none`) everyone using the server shares the same knowledge bases and chats; turn sign-in on before sharing the URL.
