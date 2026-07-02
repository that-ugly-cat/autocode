# AutoCode — User Guide

AutoCode is a research tool for qualitative coding with two engines: an **LLM engine** (Claude reads every segment and applies your codebook, with a rationale for each assignment) and a **dictionary engine** (deterministic lemma matching, free and instant). You stay in control of the codebook and the final analysis — AutoCode does the first systematic pass.

---

## 1. Getting started

1. **Register** with your email and log in.
2. **Set up two-factor authentication.** It is mandatory: on first login you are sent to an enrollment page — scan the QR code with an authenticator app (Ente Auth, Google Authenticator, Aegis…) and confirm with the 6-digit code. You then get a set of **backup codes**: save them, each works once if you lose your device. From then on, every login asks for the current code (or a backup code). You can regenerate the backup codes from your Profile; an admin can reset your 2FA if you are locked out.
3. If you plan to use the LLM engine, open **Profile** and save your **Anthropic API key**. It is stored encrypted and used only for runs you start; costs are tracked per run and shown in your profile. The dictionary engine needs no key.
4. Create a **workspace**: one workspace per study.

## 2. Workspaces

A workspace bundles a corpus, a codebook and a run history. Only one thing is fixed at creation:

- **Corpus type** — `DOCX documents` (interviews, field notes) or `Excel free-text columns` (survey responses). Fixed once the corpus contains documents.

The **coding unit** — what counts as one unit of analysis — is **chosen on the Runs page each time you launch a run** (and snapshotted on that run, so history stays consistent). The available units depend on the corpus type:

| Corpus type | Unit | Meaning |
|---|---|---|
| DOCX | Utterance (regex) | One speaker turn, e.g. `SPEAKER [HH:MM:SS]: text` (regex per document convention) |
| DOCX | Paragraph | One DOCX paragraph |
| DOCX | Sentence | One sentence (spaCy, per-document language) |
| DOCX | Whole document | One file = one unit |
| Excel | Cell | One answer = one unit |
| Excel | Sentence | One sentence within an answer (the full answer is shown to the model as context) |

You can **invite collaborators** by email (they must be registered). Members can upload documents, edit the codebook and start runs; only the owner (and admins) can change settings, remove documents or delete the workspace.

For the LLM engine, fill in the **study context** in Settings: a short description of the study (research question, population, setting). It goes into the coding prompt — runs are blocked without it.

**Duplicate a workspace** (Settings → Duplicate) to reuse the same corpus with a different codebook: it copies the settings, the members and the whole corpus — documents, files and metadata — into a new workspace. Runs are not copied; the codebook is copied only if you tick the box.

## 3. Corpus

- **DOCX workspaces**: upload `.docx`, `.txt` or noScribe `.html` files. Office lock files (`~$…`) are skipped automatically; legacy `.doc` is rejected — convert to `.docx` first.
- **Transcript conventions are auto-detected per document**: at upload, AutoCode tries the built-in conventions (`SPEAKER [HH:MM:SS]: text`, f4 with trailing `#h:mm:ss-d#` timestamps, plain `SPEAKER:` as produced by noScribe, leading `[HH:MM:SS] SPEAKER:`) plus any custom convention saved in the workspace. Mixed corpora are fine — each document carries its own convention. Header lines before the first real turn (institute, contact, "Transcribed with…") are treated as front matter and never coded.
- If no convention fits, the document is flagged **⚠ unsegmented**: open **Setup**, look at the first lines, write a regex with named groups (`(?P<speaker>…)`, `(?P<text>…)`, optional `(?P<ts>…)`) — or let **Suggest regex (LLM)** draft one — name it and save: it joins the workspace library and is tried automatically on future uploads.
- **Speakers and roles**: speaker labels are normalized (`v1.` = `V1:` = V1) and continuation paragraphs inherit the current speaker. Each document maps labels to **roles** (interviewer / participant / other) — defaults are guessed (I → interviewer), and you can correct them in Setup, because who the interviewer is may differ per document. Speaker awareness is **orthogonal to the unit**: when a document has a convention, the speakers are parsed first and the chosen unit then sub-divides each turn — so role exclusion works in `utterance`, `sentence` and `paragraph` units alike.
- **Excluding roles** is a per-run choice on the Runs page (see §5): excluded units (typically the interviewer's questions) are never coded and never sent to the LLM, but they stay in the context window so the model still sees the question. They appear as *excluded* in the coverage.
- **Excel workspaces**: upload an `.xlsx`, pick the sheet and the free-text column(s) to code. Each selected column becomes a corpus item; every non-empty cell is one respondent. Optionally pick a **group column** — a categorical column (e.g. the experimental condition) whose value tags each respondent: each text column is then split into one document per group value, so the analysis can compare groups. Codings keep the original spreadsheet row, so you can always trace a result back to the respondent.
- Each document gets a **language** (auto-detected, correct it if needed; sentence segmentation uses it) and an optional **group** label (e.g. interview module) used in exports and grouped analysis.
- The **segmentation preview** shows how text will be split — choose a unit in *Preview as*, then paste an excerpt (DOCX) or preview the first cells of a document.
- **Export / import the corpus** as one portable `.autocorpus` file (documents plus language, group, convention and roles, and the workspace's custom conventions). Import it into any workspace of the same type — e.g. to move a corpus to another server or hand it to a colleague. The codebook is not included (it has its own Excel export).

## 4. Codebook

A code has a **label**, a **description** (what the model reads — make it count), an optional **example**, and optional **dictionary expressions** per language.

- **Import from Excel**: columns `Code`, `Description`, `Example` (optional) and `Expressions_en` / `Expressions_de` / `Expressions_fr` / `Expressions_it` (optional, `;`-separated; quoted expressions like `"on my own"` work inside the cell). Duplicates are detected on a normalized label and skipped, with a preview before anything is written. **Export to Excel** produces the same format — full round trip.
- **Model-proposed codes**: during an LLM run the model may propose new codes; they enter the codebook with a visible badge and are fully editable. Exact duplicates are merged automatically.
- **Deletion is soft**: removed codes disappear from the active codebook but historical codings remain readable.
- The **Extracts** count next to each code opens all its coded segments, with the rationale of every assignment, grouped by document and filterable by run.

## 5. Runs

Pick the **documents**, the **coding unit** (the dropdown here is the segmentation choice — see §2), the **engine**, and — for DOCX with conventions — any **roles to exclude**, then start. The run executes in the background — you can close the browser; progress is committed document by document, shown with a live **progress bar** and a rough time-remaining estimate. **Estimate time / cost** gives a ballpark (segments, tokens, USD, duration) before launching — a rough figure extrapolated from a sample of documents, not a cap. The owner (or an admin) can **delete a run** from its page; the codebook and documents stay.

**LLM engine (Claude)**
- Before the run starts you confirm a short **data-protection notice** — each excerpt is sent to an external service (see §8).
- Parameters: model, parallel workers, and a **context window** (how many preceding segments the model sees, not coded — only for sequential units).
- The codebook and the study context go into every call; prompt caching keeps costs nearly flat on large corpora.
- For each segment the model applies existing codes, proposes new ones, or leaves it uncoded — always with a rationale.
- An **empty codebook is allowed**: the model then does pure inductive coding from your study context (you get a warning). The dictionary engine, by contrast, needs codes with expressions.

**Dictionary engine (NLP)**
- No API key, no cost, instant. Expressions are lemmatized (spaCy, per-document language) and matched sentence-by-sentence inside each unit. Two matching modes, chosen by syntax:
  - **No quotes** (`control body`) — *concept co-occurrence*: all content lemmas must appear in the same sentence, any order; stop words are ignored. Robust to rephrasing.
  - **With quotes** (`"go where I want"`) — *exact construction*: the lemmatized sequence must appear contiguously and in order, stop words included. Use it for idioms and first-person agency constructions (`"on my own"`, `"my own decisions"`) where pronouns and possessives are the signal. Lemmatization still covers inflections: `"give up driving"` matches "gave up driving".
- Use **Preview lemmas** in the expression editor: it shows what each expression reduces to and warns about expressions that are too generic (single content lemma) or inert (only stop words).
- Each match records which expressions fired and a **relevance score** (matches per unit) — useful to rank the best narrative examples per code.
- A warning tells you if some codes have no expressions for a corpus language, or if documents lack a language. Warnings do not block the run.
- The method behind this engine — multilingual lemma dictionaries with sentence-level matching — is described in detail, with a full use case, in: Spitale G, Seinsche J, Visscher RMS, Schöpf-Lazzarino A, Jurisic J, Germani F, Alder E, Biller-Andorno N, Ribi K, Schwind B. *Financial Burden in Adults With Chronic Illness in Switzerland: A Secondary Analysis of Qualitative Interviews Using Natural Language Processing and Topic Modeling.* JMIR Form Res 2026;10:e79290. [doi:10.2196/79290](https://formative.jmir.org/2026/1/e79290/)

**Reliability**
- If some documents fail, the run completes anyway and lists them; **Retry failed documents** re-codes only those.
- A server restart mid-run marks it as interrupted; retry resumes from the failed documents.

## 6. Reviewing results

- **Run page**: live status per document, segments / uncoded counts, codings, new codes, tokens and cost.
- **Codebook → Extracts**: read every coded segment with its rationale (and relevance score for dictionary runs).
- The **uncoded segments** matter too: the export includes every processed unit with its status and, for LLM runs, the model's reason for *not* coding it — your negative cases.
- **Analysis page** (button on every run with data): codings per code, normalized comparison across document groups (with deviation from the cross-group mean), code co-occurrence matrix, documents × codes with coverage, and lemma frequencies over the coded segments per language (raw and normalized, filtered by the workspace's per-language stoplists — set them in Settings to remove transcription artifacts and empty verbs). Dictionary runs add **expression firing counts** (split per language, with the same Code and Group drill-down as the lemmas) and the **top extracts** per code by relevance score (collapsible per-code accordions, filterable by code and group). Every chart downloads as publication-ready PNG or PDF; all underlying data exports as one Excel workbook. The analysis is computed on demand — on a large corpus a progress bar shows the lemma pass — then cached (runs are immutable); use **Recompute** after changing the stoplists.
- **Lemma frequencies drill down on three levels**: overall, per code, and per code × group (use the Code and Group selectors on the chart). Multi-coded segments count in every one of their codes, so per-code totals exceed the overall total by design. Cells with fewer than 50 lemmas are flagged as *low volume* — treat their rankings as anecdote, not data. One honest caveat for **dictionary runs**: per-code lemma lists are partly circular (the expressions you searched with will dominate the vocabulary of their own code); for LLM runs the measure is fully genuine.

## 7. Export

From a completed run:

| File | Content |
|---|---|
| **Excel** | `codings` (one row per assignment: document, group, row, segment, code, rationale, matched expressions, relevance score, offsets) · `segments` (full coverage, coded or not, with no-code rationales) · `codebook` (with expressions) |
| **QDC** | The current codebook in REFI-QDA format |
| **QDPX** | Full REFI-QDA project: documents and codings anchored at character level. Tested with MAXQDA; the standard (ISO 24277) also opens in NVivo and ATLAS.ti. Excel sources carry `[R5]` row markers in the text so you can see which respondent a passage belongs to |

Charts, lemma frequencies, topic modeling: do them in a notebook from the Excel export — the `segments` sheet contains every text.

## 8. Data protection

AutoCode processes whatever you upload — **it is up to you, the researcher, to make sure that what you upload is appropriate to process**. A few facts to base that judgement on:

- With the **LLM engine**, every coded segment (plus its context window and your study context) is sent to the **Anthropic API**, an external processor. Personal data and special categories of data (health, sexuality, political opinions, religion…) should **not** travel through this pipeline unless your ethics approval, your data management plan and your legal basis explicitly cover it.
- The **dictionary engine runs entirely on the server** — nothing leaves the machine. For sensitive corpora it is the appropriate first choice.
- **Pseudonymize before uploading**: replace names, places, dates and any identifying detail in transcripts and survey exports *before* they enter the corpus. Coding quality does not depend on real identities.
- Uploaded documents are stored on the server for the lifetime of the workspace and are visible to all its members. Deleting a document (or the workspace) removes its files and codings.
- Compliance with GDPR, your institutional requirements and the conditions of your ethics approval is the researcher's responsibility, not the tool's. When in doubt, ask your data protection officer — before uploading, not after.

## 9. Good practices

- **Description quality drives coding quality** (LLM engine): a one-line vague description yields vague coding. Write what a human coder would need.
- **Pilot first**: run a small subset, review the extracts, refine codebook and study context, then launch the full corpus.
- **Dictionary + LLM**: the dictionary engine is great for screening (where does the corpus talk about X?) and for transparent, reproducible counts; the LLM engine for interpretation. Run both on the same corpus and compare.
- AutoCode produces a **first pass to refine, not a finished analysis**. The QDA export exists precisely so the human work continues in your usual tools.
