# SurMore Assistant

Turn your lesson slides, PDFs, Word documents and spreadsheets into a chatbot that answers from them.
You put files into **knowledge bases**, check and fix the text the app reads from them, then ask
questions. Every answer points to the exact passages it came from and shows the diagrams on those pages.

- **Many knowledge bases**: one per course, project or topic. A chat can use one or several at once.
- **Most office files**: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx/.xlsm), CSV, text, Markdown and images.
- **Reads scans and diagrams**: scanned pages, photos, infographics and picture-only slides are read by an NVIDIA
  vision model, which copies their text and describes their diagrams.
- **You stay in control of the text**: every file is shown page by page, and you can correct any part.
- **Answers you can check**: numbered citations open the passage, the original file, or the page in the app.
- **Diagrams with answers**: the diagrams and pictures in your files are saved and shown under answers that cite them.

## How it works

```
  Your files        →   Readable text      →   Search index      →   Chat
  PDF, Word,            one section per        passages saved        the AI answers from the
  slides, Excel,        page, slide or         so they can be        passages that best match
  images                heading; you can       found by meaning      your question, cites them
                        edit any of it         and by keyword        and shows their diagrams
```

1. **Upload.** You drop files into a knowledge base.
2. **Read.** The app turns each file into Markdown (plain text with headings, lists and tables). Pages that are
   pictures rather than text, such as scans, photos, infographics and image-only slides, are read by NVIDIA's
   vision model. Repeated headers, footers and page numbers are removed, and the diagrams and pictures in the
   file are saved.
3. **Review.** You open a file to see its text section by section, next to where it came from (page 3, slide 7,
   "Chapter 2"…), with its diagrams. If something was read wrong, or you want to add context, you edit it.
4. **Index.** The text is split into short passages, and an NVIDIA embedding model turns each passage into a
   vector, a list of numbers that captures what the passage means. They're stored in PostgreSQL with pgvector.
   Edits are re-indexed the moment you save them. Tables of contents are left out, because they match almost
   every question without answering any.
5. **Ask.** The app looks for passages close to your question in meaning, plus passages that share its keywords.
   An NVIDIA reranking model then scores each one against the question, and the best 6 are kept. This is called
   retrieval-augmented generation (RAG).
6. **Answer.** Those passages go to an NVIDIA-hosted language model, which is told to answer only from them and
   to cite them as `[1]`, `[2]`… The answer appears word by word, with the diagrams from the cited pages below it.

Built with FastAPI (Python), PostgreSQL + pgvector, React, and models from [build.nvidia.com](https://build.nvidia.com).

## Get an NVIDIA API key

The app uses NVIDIA's hosted models for reading, searching and answering, so it needs a free API key.

1. Go to [build.nvidia.com](https://build.nvidia.com) and sign in, or create a free NVIDIA account with your
   email. No credit card is needed.
2. Open [build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys) (or click
   **Get API Key** on any model's page).
3. Generate a new key and copy it. It starts with `nvapi-` and is shown only once, so paste it somewhere safe
   straight away.

The free tier is meant for trying things out, so it has rate limits, and responses are occasionally slow when
NVIDIA's service is busy. Keep the key private: anyone with it can use your NVIDIA account.

## Run it on your computer

You need:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/), for the database
- [Python](https://www.python.org/downloads/) 3.12 or newer
- [Node.js](https://nodejs.org/) 20.19+ or 22.12+
- Your NVIDIA API key (see above)

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
NVIDIA_LLM_MODEL=nvidia/nemotron-3.5-super-vl-120b-a12b
```

`NVIDIA_LLM_MODEL` is the model that writes the answers; any chat model on build.nvidia.com works. The one above is
fast, and it's the same model the app uses to read scans and diagrams. Everything else has working defaults;
`backend/.env.example` explains each setting. Keep `.env` private: it's already excluded from git.

**4. Start the backend** (in one terminal)

Windows:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --reload
```

macOS / Linux:

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --reload
```

Wait for `Application startup complete`.

**5. Start the frontend** (in a second terminal)

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5180**.

Next time, you only need `docker compose up -d`, the `uvicorn` line from step 4 (run inside `backend`) and
`npm run dev` (inside `frontend`). The backend reads `backend/.env` when it starts, so restart it after you change
that file.

## Using the app

**Create a knowledge base.** Click **+** next to *Knowledge bases* in the sidebar and give it a name, like
"Biology 101".

**Add files.** Drag files onto *Drop files here or choose files*. Each file shows its progress while it's read and
indexed. A long PDF can take a few minutes, and you can keep using the app meanwhile. While a file is being read,
it isn't used for answers yet.

**Check the text.** Click a file's name to open it. Each page, slide or heading is a section, with its diagrams
underneath:

- **Edit** opens the Markdown with a live preview. **Save changes** updates the search index straight away.
- **Run OCR** reads a PDF page or image again, which is useful when a page came out empty or garbled.
- Sections marked *OCR* were read from an image, and sections marked *Edited* were changed by you.
- **Read the file again** at the top reads the whole file again. It replaces your edits.
- Click a diagram to see it full size.

**Chat.** Click **New chat**, or **Chat with this knowledge base** on a knowledge base's page. Under *Answer from*,
pick which knowledge bases to use, then ask. Diagrams from the cited pages appear under the answer, numbered like
the sources. Click a numbered source to see the passage, then **Show in document** to jump to that section, or
**Open PDF file** to see the original page. If you pick no knowledge base, the model answers from its general
knowledge.

### Supported files

| Type | How it's split | Notes |
|---|---|---|
| PDF | one section per page | scanned pages are read with OCR; embedded diagrams and pictures are saved |
| Word (.docx) | by heading | keeps lists, tables, bold and links; pictures are saved |
| PowerPoint (.pptx) | one section per slide | includes speaker notes; picture-only slides use OCR; large pictures are saved |
| Excel (.xlsx, .xlsm), CSV | blocks of rows | each sheet becomes a table |
| Text, Markdown | by heading or length | |
| Images (PNG, JPG, WebP, TIFF, BMP) | one section | read with OCR, and kept as a diagram |

Old formats (.doc, .ppt, .xls) aren't supported. Save them as .docx, .pptx or .xlsx first. Files can be up to
200 MB.

Diagrams drawn as shapes rather than embedded as images (Word and PowerPoint charts, SmartArt, vector drawings
in PDFs) aren't saved, but OCR still reads their text on pages that need it.

## Settings

All settings are in `backend/.env`; `backend/.env.example` lists every one with an explanation. The ones you're
most likely to change:

| Setting | Default | What it does |
|---|---|---|
| `NVIDIA_LLM_MODEL` | (none, required) | The model that writes answers |
| `NVIDIA_EMBEDDING_MODEL` | `nvidia/nemotron-3-embed-1b` | Turns passages into vectors for search |
| `NVIDIA_OCR_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` | Reads scans and diagrams. `nvidia/nemotron-parse` also works, but invents words on diagrams |
| `NVIDIA_RERANK_MODEL` | `nvidia/llama-nemotron-rerank-vl-1b-v2` | Picks the best passages for each question. Leave it empty to turn reranking off |
| `OCR_MODE` | `auto` | `auto`: only pages and slides without readable text, plus images. `always`: every PDF page (one API call per page). `off`: never |
| `EXTRACT_FIGURES` | `true` | Save diagrams and pictures from files |
| `LLM_MAX_TOKENS` | `1024` | Longest answer. Answers that hit it end with a note saying so |
| `RETRIEVAL_TOP_K` | `6` | How many passages each answer is based on |

## Good to know

- If you change `NVIDIA_EMBEDDING_MODEL` after uploading files, those files show **Needs re-indexing**. Click
  **Re-index** on each one. Your edits are kept.
- NVIDIA retires models from time to time. If uploads or answers fail with an error mentioning "end of life", pick
  a current model on [build.nvidia.com](https://build.nvidia.com) and update `backend/.env`.
- Your data lives in the Docker volume `surmore-pgdata` (database) and in `backend/data/uploads/` (original files
  and diagrams).
- Interactive API documentation is at http://127.0.0.1:8000/docs while the backend runs.
