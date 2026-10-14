# SurMore Assistant

Turn your lesson slides, PDFs, Word documents and spreadsheets into a chatbot that answers from them.
You put files into **knowledge bases**, check and fix the text the app reads from them, then ask
questions. Every answer points to the exact passages it came from.

- **Many knowledge bases**: one per course, project or topic. A chat can use one or several at once.
- **Most office files**: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xlsm), CSV, text, Markdown and images.
- **Reads scans**: scanned pages, photos and picture-only slides go through OCR (text recognition).
- **You stay in control of the text**: every file is shown page by page, and you can correct any part.
- **Answers you can check**: numbered citations open the passage, the original file, or the page in the app.

## How it works

```
  Your files        →   Readable text      →   Search index      →   Chat
  PDF, Word,            one section per        passages saved        the AI answers using the
  slides, Excel,        page, slide or         so they can be        passages that best match
  images                heading; you can       found by meaning,     your question, and cites
                        edit any of it         not just keywords     each one it used
```

1. **Upload.** You drop files into a knowledge base.
2. **Read.** The app turns each file into Markdown (plain text with headings, lists and tables). Pages that are
   pictures rather than text, such as scans, photos and image-only slides, are read with NVIDIA's OCR model.
3. **Review.** You open a file to see its text section by section, next to where it came from (page 3, slide 7,
   "Chapter 2"…). If something was read wrong, or you want to add context, you edit it.
4. **Index.** The text is split into short passages, and an NVIDIA embedding model turns each passage into a
   vector, a list of numbers that captures what the passage means. They're stored in PostgreSQL with pgvector.
   Edits are re-indexed the moment you save them.
5. **Ask.** Your question is turned into a vector the same way, and the passages closest in meaning are pulled
   from the knowledge bases you picked. This is called retrieval-augmented generation (RAG).
6. **Answer.** Those passages go to an NVIDIA-hosted language model, which is told to answer only from them and
   to cite them as `[1]`, `[2]`… The answer appears word by word as it's written.

Built with FastAPI (Python), PostgreSQL + pgvector, React, and models from [build.nvidia.com](https://build.nvidia.com).

## Run it on your computer

You need:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/), for the database
- [Python](https://www.python.org/downloads/) 3.12 or newer
- [Node.js](https://nodejs.org/) 20.19+ or 22.12+
- A free NVIDIA API key from [build.nvidia.com](https://build.nvidia.com) (starts with `nvapi-`)

**1. Get the code**

```bash
git clone https://github.com/Takaguya/SurMore_Assistant.git
cd SurMore_Assistant
```

**2. Start the database**

```bash
docker compose up -d
```

This runs PostgreSQL with pgvector on port 5433. Your data is kept between restarts.

**3. Add your NVIDIA key**

Copy `backend/.env.example` to `backend/.env`, then fill in two lines:

```env
NVIDIA_API_KEY=nvapi-...
NVIDIA_LLM_MODEL=meta/llama-3.3-70b-instruct    # any chat model listed on build.nvidia.com
```

Everything else has working defaults; `backend/.env.example` explains each setting. Keep `.env` private: it's
already excluded from git.

**4. Start the backend** (in one terminal)

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**5. Start the frontend** (in a second terminal)

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5180**.

Next time, you only need `docker compose up -d`, then `uvicorn app.main:app --reload` (with the virtual
environment activated) and `npm run dev`.

## Using the app

**Create a knowledge base.** Click **+** next to *Knowledge bases* in the sidebar and give it a name, like
"Biology 101".

**Add files.** Drag files onto *Drop files here or choose files*. Each file shows its progress while it's read and
indexed. A long PDF can take a few minutes, and you can keep using the app meanwhile.

**Check the text.** Click a file's name to open it. Each page, slide or heading is a section:

- **Edit** opens the Markdown with a live preview. **Save changes** updates the search index straight away.
- **Run OCR** reads a PDF page or image again, which is useful when a page came out empty or garbled.
- Sections marked *OCR* were read from an image, and sections marked *Edited* were changed by you.

**Chat.** Click **New chat**, or **Chat with this knowledge base** on a knowledge base's page. Under *Answer from*,
pick which knowledge bases to use, then ask. Click a numbered source under the answer to see the passage, then
**Show in document** to jump to that section, or **Open PDF file** to see the original page. If you pick no
knowledge base, the model answers from its general knowledge.

### Supported files

| Type | How it's split | Notes |
|---|---|---|
| PDF | one section per page | scanned pages are read with OCR |
| Word (.docx) | by heading | keeps lists, tables, bold and links |
| PowerPoint (.pptx) | one section per slide | includes speaker notes; picture-only slides use OCR |
| Excel (.xlsx, .xlsm), CSV | blocks of rows | each sheet becomes a table |
| Text, Markdown | by heading or length | |
| Images (PNG, JPG, WebP, TIFF, BMP) | one section | read with OCR |

Old formats (.doc, .ppt, .xls) aren't supported. Save them as .docx, .pptx or .xlsx first. Files can be up to
200 MB (`MAX_UPLOAD_MB`).

### OCR settings

`OCR_MODE` in `backend/.env` controls when OCR runs:

- `auto` (default): only pages and slides without readable text, plus image files.
- `always`: every PDF page, for the cleanest structure. This makes one API call per page.
- `off`: never.

## Share it with other people

Run locally, the app has no sign-in, so everyone using your server shares the same knowledge bases and chats.
To put it online for others, turn on accounts: each person then signs in with Google or an emailed link, sees
only their own knowledge bases, files and chats, and has usage limits so they can't use up your NVIDIA quota.

A setup that costs nothing to start:

| Piece | Service | What it holds |
|---|---|---|
| Database and sign-in | [Supabase](https://supabase.com) free plan | knowledge bases, text, search index, chats, accounts |
| Uploaded files | [Cloudflare R2](https://developers.cloudflare.com/r2/) (10 GB free) | the original PDFs, slides and spreadsheets |
| Web server | [Render](https://render.com) free plan, or any host that runs Docker | the app itself, on one URL |

**1. Supabase**

1. Create a project. Under **Connect**, copy the **Session pooler** connection string. That's `DATABASE_URL`.
2. In **Project Settings > API Keys**, copy the project URL (`SUPABASE_URL`) and the `anon` / publishable key
   (`SUPABASE_ANON_KEY`).
3. In **Authentication > URL Configuration**, set **Site URL** to your public address (for example
   `https://surmore-assistant.onrender.com`) and add it under **Redirect URLs**.
4. In **Authentication > Providers**, email is on by default. To offer Google too, enable it with a Google OAuth
   client ID and secret, then set `AUTH_PROVIDERS=email,google`.

The free plan holds 500 MB and pauses after a week with no visits. To fit more documents, set
`NVIDIA_EMBEDDING_DIMENSIONS=1024` or `512` before uploading.

**2. Cloudflare R2**

Create a bucket and an API token with **Object Read & Write** access to it. Then set `STORAGE_BACKEND=s3`,
`S3_BUCKET`, `S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com`, `S3_ACCESS_KEY_ID` and
`S3_SECRET_ACCESS_KEY`. Any S3-compatible storage works the same way.

**3. Render**

Fork or push this repository to your GitHub account. In Render, click **New > Blueprint** and pick the repository.
`render.yaml` sets everything up, and Render asks you for each secret: the database URL, the Supabase and NVIDIA
keys, and the R2 details. Your app is then live at its Render URL.

Render's free server sleeps after 15 idle minutes, so the first visit after that takes about a minute.

To use another host (Railway, Fly.io, Google Cloud Run, a VPS), build the `Dockerfile` and set the same settings.
To try the production version on your own machine:

```bash
docker build -t surmore-assistant .
docker run -p 8000:8000 --env-file backend/.env surmore-assistant
```

Inside the container, `localhost` means the container itself. To use the local database from step 2, set
`DATABASE_URL=postgresql://surmore:surmore@host.docker.internal:5433/surmore`.

Usage limits (messages per minute and per day, uploads, OCR runs, storage per account) are in `backend/.env`;
`0` turns a limit off. With `ENVIRONMENT=production`, the app won't start until sign-in is set up, so it can't
go online open to everyone by mistake.

## Good to know

- Knowledge bases and chats created before you turned on sign-in belong to a local user, so they don't appear
  for signed-in accounts.
- If you change `NVIDIA_EMBEDDING_MODEL` after uploading files, those files show **Needs re-indexing**. Click
  **Re-index** on each one. Your edits are kept.
- Interactive API documentation is at http://127.0.0.1:8000/docs while the backend runs locally.
