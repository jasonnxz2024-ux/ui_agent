# Static Figma design import and local visual repair

Status: design plan. The static-design importer and agent repair loop are not implemented yet.

## Observed sample

- Source: `https://www.figma.com/design/JRvPKo0M6T08wLMWjpaJlh/cluster?node-id=0-1`
- `0:1` is the page; `4:19` is the target `SPORT` frame, 800 x 480.
- Read-only Figma inspection found 162 nodes: 1 frame, 117 vectors, 33 groups,
  5 instances, 5 text nodes and 1 boolean operation. Several visible labels are
  outlined vectors rather than editable text.
- The frame screenshot is a validation reference only, never an implementation
  asset. Preserve the source ID and bounds of every descendant node.

## Input and IR

1. Add a separate `figma_design` source adapter. The selected standalone-EXE
   access method is Figma OAuth login; Codex's Figma connection is not embedded
   in the EXE. Figma's token exchange requires a client secret and an external
   callback service, so the secret and refresh token belong on an OpenHmi
   Station backend, never inside the EXE. Register a Figma OAuth app with
   `file_content:read`; a public app needs Figma review. Figma authorization is
   separate from any model-provider key.
2. Resolve a page URL to its frame(s), keeping original dimensions and node
   IDs. Reject ambiguous multi-frame selection instead of guessing.
3. Build a source-node tree with type, parent, order, bounds, transforms,
   visibility, text, fills, strokes, effects, vector geometry and asset refs.
   Record every source node in the audit, including non-rendered groups.
4. Classify each visible leaf through the adapter as native, custom, partial or
   fallback. Native text/shapes/images stay editable; complex vector paths may
   become individual SVG/image assets or custom draw commands, never one
   full-frame PNG. Unsupported effects are explicit blockers/warnings.
5. Generate AiBuilder `custom.c`/`custom.h` and a Windows SDL simulator from
   the same render plan. Keep source-node IDs in generated comments and audit.

## Verification and repair

- Static Figma source: show the frame reference persistently next to a freshly
  rebuilt SDL preview. React/Figma Make source: keep the current live Browser
  reference and synchronized interaction-state captures.
- Retain the deterministic visual, region and structural gate; no LLM may
  override a failed gate. A failed simulator build is a hard blocker.
- Replace cloud BYOK visual calls with an explicit local agent runner. The
  runner receives screenshots, region metrics, source-node mapping and audit
  only; it proposes bounded, schema-validated changes to the intermediate
  model or adapter parameters. Each round shows the proposed changes and SDL
  result. Preserve prior versions and roll back any regression.
- A coding agent (for example Codex CLI or Aider) still requires a model and
  user authorization. An entirely local option may use an Ollama vision model,
  but is optional and must pass a hardware/capability check. No model is
  required to run the deterministic comparison or manual preview.
- Remove the old BYOK UI/endpoints only after the new runner and preview flow
  pass regression tests. Do not turn the existing one-round validator into a
  misleading claim of automatic repair.

## Acceptance criteria

- All 162 sample nodes have traceable source identities; no full-frame image is
  emitted into the generated UI.
- Text, vectors, groups and instances are counted separately in the audit.
- SDL output uses 800 x 480 and is compared against the Figma frame reference.
- At least one low-score case produces a visible, bounded repair proposal and
  another preview round; a no-repair case reports why it stopped.
- `cluster`, `climate`, `test`, `print` and `meter` React fixtures retain their
  existing scanner/adapter behavior; no project-name special cases.
