# UI Asset Pipeline

This project turns approved AI-generated UI references into reusable image assets and editable pages.
The image model is called through an OpenAI-compatible HTTP API. It is not coupled to Codex.

## Browser workspace

The complete workflow is available from one localhost browser application. Install the dependency and start it:

```powershell
python -m pip install -r requirements.txt
python app.py --port 8766
```

Then open `http://127.0.0.1:8766`. The service only accepts localhost requests.

The browser workspace provides page creation, switching and renaming, API configuration, design-spec editing, AI prompt generation, reference generation and approval, AI-assisted two-level region marking with mandatory human confirmation, deterministic crops, separate local/AI processing actions, versioned asset review, source editing, fixed-canvas preview, and comparison iterations. Paid model calls require an explicit confirmation in the browser.

Stages 6 and 7 use a browser-created Codex task plus a local MCP server. The browser never launches Codex: create a task, copy its instruction, manually open a new Codex session, and let that session claim the exact task id through MCP. Source updates and comparison artifacts return to the browser automatically.

The API key is write-only in the workspace UI. It remains in `.env`; `/api/state` reports only whether a key is configured. Browser source editing is restricted to `src/index.html`, `src/styles.css`, and `src/app.js`, and file previews are restricted to known project output directories.

## Pipeline

```text
project design-spec.json
    -> text-model API: generate gpt-image prompt
    -> gpt-image API: generate reference page
    -> human approves page
    -> AI suggests or human draws green crop regions and red target boxes
    -> human classifies and confirms every target
    -> local transparent / complete crop / AI transparent / background repair
    -> preserve original crop plus immutable processed versions
    -> human reviews images and code elements, names them, and confirms placement/layer
    -> Codex creates HTML/React from spec + manifest
    -> browser render + overlay/diff
    -> Codex reviews comparison and changes code
```

## Directory layout

```text
project/
  workspace/project.json           # page catalog and active page id
  workspace/design-spec.json       # shared design rules for every page
  workspace/settings.json          # shared image sizes and processing settings
  workspace/tasks.db               # persistent Codex tasks and event history
  design-spec.json                 # compatibility mirror of the shared rules
  reference/reference-page.png     # approved page reference
  regions/regions.json             # manually drawn regions and global coordinates
  regions/crops/                    # crops created from the reference page
  assets/raw/                       # one gpt-image result per AI extraction region
  assets/split-web/originals/       # immutable crops before processing
  assets/split-web/local/           # versioned local transparent results
  assets/split-web/complete/        # versioned complete crops
  assets/split-web/background-repair/ # versioned AI repair results
  assets/approved/                  # user-approved split PNG assets
  assets/asset-manifest.json       # asset names, roles, hierarchy, coordinates
  src/                              # generated HTML/React source
  artifacts/iterations/             # render, overlay, diff, metrics, adjustments
  runs/gpt-image/                   # immutable API request/response records
  pages/<page-id>/                  # every newly created page is isolated here
    state.json
    prompts/                        # generated prompt and page-specific requirements
    reference/
    regions/
    assets/
    src/
    artifacts/
    runs/
  scripts/                          # API client, cropper, comparer, orchestrator
```

## Codex MCP setup

Install dependencies, then register the stdio server once with the local Codex client:

```powershell
python -m pip install -r requirements.txt
codex mcp add ui_asset_pipeline -- python "C:\Users\Administrator\Documents\Codex\2026-09-09\new-chat-3\outputs\ui-asset-pipeline\mcp_server.py"
codex mcp get ui_asset_pipeline
```

New Codex sessions can then call `claim_task`, `get_task_context`, `get_source_files`, `write_source_files`, `create_comparison`, `get_comparison`, `submit_for_review`, `report_task_error`, and `release_task`. A claim is scoped to the MCP process. If a session disappears before releasing a task, cancel it in the browser and create a new task.

The MCP server never reads `.env`. It exposes only frozen task context, approved asset paths, the three source files, and immutable comparison artifacts. Browser and MCP writes use a source revision hash so stale edits cannot overwrite newer work.

The existing `game-home` page remains in the project root for backward compatibility. It is registered as legacy storage and is never moved, copied, or deleted. Newly created pages use `pages/<page-id>/`; switching pages changes page-owned workflow reads and writes to that page root. The design specification, model configuration, canvas sizes, quality, crop padding, comparison threshold, and Codex calibration limit are project-level settings shared by every page. Background jobs capture their page id when queued, so switching pages while a task runs cannot redirect its output.

## API configuration

Copy `.env.example` to `.env` and set the endpoint, key, and model. The endpoint defaults to the compatible gateway shown in the request:

```text
GPT_IMAGE_BASE_URL=http://154.12.91.166:3000/v1
GPT_IMAGE_API_KEY=replace-image-key
GPT_IMAGE_MODEL=gpt-image-2
GPT_TEXT_BASE_URL=http://154.12.91.166:3000/v1
GPT_TEXT_API_KEY=replace-text-key
GPT_TEXT_MODEL=replace-text-model
```

The text gateway uses the OpenAI-compatible `POST /chat/completions` endpoint. The application sends the active page name, the shared project design-spec JSON, and that page's requirements, then stores the returned English image prompt in the page's `prompts/reference-page.txt`. Page requirements are retained in `prompts/reference-page.notes.txt`. Request and response records are written under that page's `runs/gpt-text/` without authorization headers or API keys. The shared settings are edited from the `项目设置` entry at the lower-left of the browser workspace.

The API client sends the same JSON fields as the supplied curl example: `model`, `prompt`, `size`, `quality`, and `n`.

The integration follows the official OpenAI Image API wire format and changes only the API host:

```bash
curl -X POST "http://154.12.91.166:3000/v1/images/edits" \
  -H "Authorization: Bearer $GPT_IMAGE_API_KEY" \
  -F "model=$GPT_IMAGE_MODEL" \
  -F "image[]=@region-001.png" \
  -F "prompt=Extract the intended image asset" \
  -F "size=1024x1024" \
  -F "quality=medium" \
  -F "background=transparent" \
  -F "output_format=png" \
  -F "n=1"
```

For `gpt-image-2`, the client intentionally omits `input_fidelity`; the official API fixes image inputs at high fidelity for this model.

Never commit `.env`, API keys, or raw authorization headers.

Install the only Python dependency:

```powershell
python -m pip install -r requirements.txt
```

Check authentication and configured-model visibility without generating an image:

```powershell
python scripts/check_api.py
```

Generate the initial reference page with the confirmed generation endpoint:

```powershell
python scripts/gpt_image_api.py reference/reference-page.png `
  --prompt-file prompts/reference-page.example.txt `
  --record-dir runs/gpt-image/reference-001 `
  --size 1024x1024 `
  --quality medium `
  --normalize-size
```

The raw API response remains in the run directory. `--normalize-size` ensures the approved reference canvas exactly matches the requested browser viewport even if a compatible gateway returns different embedded PNG dimensions.

## Data contracts

`regions/regions.json` stores two levels of human decisions. Green crop-region coordinates are in reference-image pixels; red target coordinates are local to their green region. Every red target also records `elementType`, `processingMode`, `name`, `parent`, `zIndex`, and `reviewStatus`. AI suggestions use `reviewStatus: "needs-review"` and are ignored by processing until a human changes them to `confirmed`.

```json
{
  "canvas": {"width": 1440, "height": 900},
  "regions": [
    {
      "id": "region-001",
      "x": 80,
      "y": 120,
      "width": 420,
      "height": 360,
      "purpose": "hero illustration",
      "approved": true,
      "targets": [
        {
          "id": "target-001",
          "x": 0,
          "y": 0,
          "width": 420,
          "height": 360,
          "purpose": "complete carousel including text and background",
          "approved": true
        }
      ]
    }
  ]
}
```

The human first marks green crop rectangles, then selects each crop and marks one or more red target rectangles. Code performs the crop reproducibly and creates both a clean crop and a `.marked.png` API input:

```powershell
python scripts/crop_regions.py reference/reference-page.png regions/regions.json regions/crops --padding 32
```

The command writes `regions.normalized.json`, preserving both the original marked rectangle and the actual padded crop box. The padded crop is API input context, not the final asset boundary.

Open `tools/region-marker.html` in a browser. Draw green crop regions, switch to red target mode, select each crop, mark every complete asset, and export `regions.json`. The marker does not crop or upload anything.

Red target boxes are authoritative. A red box around a full carousel means its background, text, badge, and button remain one complete asset. A red box around an icon means only the icon is extracted. The extraction prompt also requests restrained upscaling, edge restoration, de-noising, and blur cleanup without redesigning the source.

The browser processing center exposes each operation separately:

- `complete-crop`: preserve the whole marked composite without transparency work.
- `local-transparent`: remove only edge-connected pixels near the estimated border color. Defaults follow the `image-to-slice` RGB-distance strategy (`34` hard tolerance and `48` feather range), while retaining the safer edge-connectivity constraint.
- `ai-transparent`: call the image edit API with transparent PNG output.
- `background-repair`: call the image edit API without forcing transparency.
- `none`: record a code-rendered element and do not create a PNG.

After the edit endpoint is configured, the legacy command-line three-stage pipeline remains available:

```powershell
.\scripts\run_pipeline.ps1 `
  -ReferenceImage .\reference\reference-page.png `
  -RegionsJson .\regions\regions.json `
  -Padding 32 `
  -Workers 0
```

The stages remain independently runnable:

```powershell
python scripts/crop_regions.py reference/reference-page.png regions/regions.json regions/crops --padding 32
python scripts/extract_regions.py regions/crops/regions.normalized.json regions/crops assets/raw runs/gpt-image
python scripts/split_extractions.py regions/crops/regions.normalized.json assets/raw assets/split
```

Each edited PNG is checked against the requested canvas size. If a compatible gateway embeds a different pixel size, the saved asset is normalized automatically and the original response plus `normalization.json` remain in the run record.

Before configuring a key or making paid calls, verify the planned inputs:

```powershell
python scripts/extract_regions.py regions/crops/regions.normalized.json regions/crops assets/raw runs/gpt-image --dry-run
```

`assets/asset-manifest.json` is produced after browser review. It includes each item's classification, processing method, source and final coordinates, semantic name, parent group, z-index, approval state, processing history, immutable `versions`, and selected `activeVersion`. The review screen presents the original crop beside the processed result; saving a review never removes either version.

The complete product workflow and acceptance gates are documented in `docs/WORKFLOW_V3.zh-CN.md`.

`asset-manifest.draft.json` deliberately does **not** invent final page coordinates. The image API may recenter or rescale an extracted object, so Codex must compare the approved asset with `reference-page.png`, name it, assign hierarchy, and record its final page placement.

## Iteration artifacts

Each comparison iteration is immutable:

```text
artifacts/iterations/004/
  render.png
  overlay.png
  diff.png
  regions.json
  metrics.json
  adjustment.json
```

Codex reads the latest iteration. The image files are not manually re-uploaded; the orchestrator or the next Codex turn references these workspace paths.

Generate a comparison package after the browser renderer produces `render.png`:

```powershell
python scripts/compare_images.py reference/reference-page.png render.png artifacts/iterations/001
```

Render the included 750-design-pixel HTML implementation reproducibly with an installed Chrome or Edge browser:

```powershell
.\scripts\render_page.ps1 -Output .\artifacts\iterations\004\render.png
```

Use `-Width 375 -Height 667` for a half-scale viewport check. The page keeps the 750-design-pixel coordinate system and scales as one unit below 750 CSS pixels.

Then ask Codex to read `artifacts/iterations/001/iteration.json`. It points to the overlay, difference image, and metrics; no manual image upload is required.

## Reliability rules

- Manual crop regions are input context, not final asset bounds. Leave a 32-64px bleed around objects.
- Reference-page generation always uses one API request at a time (concurrency 1).
- Asset extraction automatically uses one worker per approved crop: N cropped images produce concurrency N. Pass `--workers N` only to override this behavior.
- Each region has independent retries and one immutable job directory.
- Validate every result is RGBA and has a non-opaque alpha channel before splitting.
- Preserve the original reference page. GPT-generated extraction may lightly redraw an asset.
- Do not turn text, cards, buttons, or ordinary layout into image assets; rebuild those in code.
