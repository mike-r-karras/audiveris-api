# Notestream MusicXML converter project handoff

## Read this first

This repository is the source of truth. Inspect the current checkout, Git
history, Python modules, tests, Docker configuration, and API contracts before
changing anything. Historical converter snapshots conflict with one another;
do not infer the current schema or feature set from filenames alone.

The authoritative target is the August 8 EasyScore/Notestream schema 1.2
converter behavior. Older schema 1.1 snapshots intentionally omitted notation
features that 1.2 now preserves. Confirm the actual checked-out baseline before
editing.

Preserve unrelated user changes. Prefer small, testable changes over rewrites.
Maintain the existing public Python converter signatures and FastAPI behavior
unless a requested change explicitly requires an API version change.

## Purpose and system boundary

This service converts uploaded scores into canonical Notestream/EasyScore JSON.
The full pipeline is:

1. PDF sheet music upload
2. Audiveris optical music recognition
3. MusicXML or compressed MXL output
4. MusicXML decompression and validation
5. Python parser and normalizer
6. Canonical camelCase EasyScore JSON
7. React/TypeScript and VexFlow 5 rendering in the Notestream application

The parser/normalizer belongs in the FastAPI conversion service. The frontend
should consume normalized JSON rather than reinterpreting raw MusicXML.

Preserve useful artifacts and provenance where supported: original upload,
Audiveris output, decompressed MusicXML, normalized EasyScore JSON, converter
version, schema version, warnings, processing metadata, and stable source IDs.

## Current input support

The API accepts:

- PDF
- `.xml`
- `.musicxml`
- compressed `.mxl`

PDF inputs run through Audiveris. XML, MusicXML, and MXL inputs bypass OCR and
convert directly. Audiveris output discovery searches for `.mxl`, `.musicxml`,
and `.xml` results. Do not assume Audiveris always emits the same extension.

Compressed MXL is a ZIP container, not plain XML. Resolve its root document
using `META-INF/container.xml` when present. Reject unsafe archive paths and do
not extract arbitrary files outside the job workspace.

The converter accepts `score-partwise` MusicXML. It must reject clearly and
consistently:

- malformed or non-XML input
- `score-timewise`
- unexpected root document types
- scores without parts
- MXL containers without a usable MusicXML root document

Do not pass compressed bytes directly to the XML parser. A prior failure
reported `Invalid MusicXML: not well-formed (invalid token): line 1, column 2`
because archive/container handling was incorrect.

## FastAPI compatibility contract

Preserve the current asynchronous conversion workflow:

- `POST /conversions` accepts the multipart upload and returns HTTP `202` with
  `jobId`, `progressUrl`, and `resultUrl`.
- `GET /conversions/{job_id}` returns job status/progress.
- `GET /conversions/{job_id}/result` returns:
  - `409` while the job is incomplete
  - `422` when conversion failed
  - `410` when the job or retained result is gone
  - the completed result when successful
- `GET /health` returns service health.

Keep multipart field names synchronized with the frontend. The upload field is
expected to be named `file`; a previous mismatch produced FastAPI `422` with
`body.file` missing.

Keep CORS behavior compatible with the browser client, including preflight
requests. Do not remove existing progress reporting or job/result URLs while
working on parser behavior.

Use timezone-aware timestamps. Import style must not shadow the `datetime`
class with the `datetime` module; a prior health endpoint failed with
`AttributeError: module 'datetime' has no attribute 'now'`.

## Canonical output contract: schema 1.2

Output is camelCase JSON and identifies schema version `1.2`. Confirm exact
field names from current types/tests before changing them.

The current converter is first-part-only. If more than one MusicXML part exists,
convert the supported part and emit the structured warning
`MULTIPLE_PARTS_IGNORED`. Do not silently pretend all parts were converted.

Within the supported part, preserve multi-staff and multi-voice timing. Handle
MusicXML `backup` and `forward` elements so each event receives an accurate
measure-relative start time.

Events should retain, as applicable:

- stable deterministic ID
- event type (`note` or `rest`)
- measure number/identity
- staff number
- voice number
- chord membership/simultaneous start
- raw MusicXML duration in divisions
- measure-relative start in quarter notes
- duration in quarter notes
- VexFlow-compatible base duration
- augmentation dots
- pitch step, octave, and chromatic alter
- written accidental metadata
- source stem direction
- source beam markers
- ties
- slurs
- normalization records

Measures/attributes should retain, as applicable:

- divisions
- time signature
- key signature
- per-staff clefs
- left and right barlines
- barline styles
- forward and backward repeat markers
- volta/ending numbers, types, and text needed for first/second/nth endings
- final light-heavy barline semantics

Do not collapse information merely because the current renderer does not yet use
it. Canonical JSON is the durable boundary between conversion and presentation.

## Timing semantics

Musical timing must be explicit and independent of VexFlow engraving.

- Quarter-note duration is the canonical neutral duration unit.
- Frontend timing currently uses `TICKS_PER_QUARTER = 480`; converter output
  must provide sufficient exact quarter-note starts/durations for deterministic
  tick conversion.
- A `<chord/>` note shares the onset of the preceding non-chord note in the same
  MusicXML cursor context.
- `backup` moves the MusicXML cursor backward; `forward` moves it forward.
- Voice/staff identity must not be inferred only from document order.
- Measure duration and time-signature duration are related but not always
  interchangeable: pickup measures, incomplete measures, polyphony, and OMR
  errors require deliberate handling.
- Do not hard-code 4/4. Preserve the numerator and denominator exactly. A 3/8
  measure represents three eighth-note beats, not three or four quarter-note
  beats.

Use rational or carefully controlled numeric arithmetic internally where
needed. Avoid cumulative floating-point cursor drift across long measures.

## Source-notation preservation

The converter should preserve source engraving/playback semantics and let the
renderer consume them. Do not replace explicit MusicXML instructions with
heuristics.

Schema 1.2 preserves:

- written stem direction
- all available beam markers and beam levels
- ties
- slurs
- written accidentals
- left/right barlines and styles
- forward/backward repeats
- first/second/nth endings
- final light-heavy barlines

Maintain the distinction between ties and slurs. Preserve identifiers/numbers
needed to pair their starts, continuations, and stops. Preserve written beam
boundaries rather than merely noting that a note is beamable.

Written repeat notation is source data. Do not expand repeated passages or
resolve playback order inside the converter. The application will eventually
support both printed-score and expanded/virtual-repeat presentation modes, so
the converter must retain immutable written order plus complete repeat/ending
metadata.

## Normalization policy

Normalization should repair safe, explainable OMR inconsistencies without
hiding source problems.

Use explicit normalization records compatible with the existing
`NormalizationRecord` shape, including:

- normalization type
- confidence
- reason
- original duration/value where applicable

Keep source evidence whenever possible. Emit warnings for uncertain repairs.
Do not silently rewrite ambiguous musical structure simply to make a measure
sum correctly.

Duration normalization may reconcile a clearly erroneous event with its
written note type/dots and surrounding measure context. It must remain
deterministic and explainable. Problems like the historical Measure 16 example
should produce a traceable normalization or warning, not an invisible guess.

## Difficulty analysis

Schema 1.2 includes a deterministic, explainable difficulty block on a 1–10
scale. Preserve it while changing the converter.

Difficulty should derive from documented score features rather than an opaque
random or model-generated value. Confirm the current factors and weights from
the implementation/tests before changing them. Keep both the final score and
the explanatory component data/warnings needed to understand it.

Difficulty calculation must not mutate musical events or source notation.

## Current intentional limitations

Confirm these against the checked-out implementation, but the current known
deferred areas include:

- multiple MusicXML parts beyond the supported first part
- tuplets
- lyrics
- grace notes

Do not partially implement one of these as an incidental side effect. If a task
requires adding one, define its canonical schema, parsing rules, warnings, and
tests explicitly.

## Stability and regression history

Converter changes have immediate renderer consequences. Prior failures included:

- treating MXL bytes as XML
- dropping or misrepresenting ties and beams
- losing repeat/endings information
- incorrect duration totals around malformed OMR output
- older Docker images producing apparently stale converter behavior
- frontend regressions when absent source metadata forced renderer heuristics

Consequently:

- Treat schema changes as compatibility changes.
- Never remove a field merely because one renderer currently ignores it.
- Preserve public converter signatures used by FastAPI.
- Keep unit tests independent of Audiveris where possible.
- Test MXL decompression separately from MusicXML parsing.
- Test the FastAPI orchestration separately from the pure converter.
- Record/log converter and schema versions so stale container deployments are
  diagnosable.
- Prefer warnings and explicit normalization records over silent data loss.

## Expected module boundaries

Confirm actual filenames before editing. Preserve or move toward these
responsibilities without forcing a rewrite:

- **MXL decompressor:** safely resolves compressed MusicXML into XML bytes/text.
- **MusicXML validator:** verifies well-formed XML, supported root type, and
  required score structure.
- **Pure converter/parser:** maps validated MusicXML to canonical EasyScore JSON
  without FastAPI or filesystem dependencies.
- **Normalizer:** applies deterministic, explainable corrections and emits
  normalization records/warnings.
- **Difficulty analyzer:** computes the explainable 1–10 difficulty block from
  canonical musical data.
- **Audiveris runner:** invokes OCR for PDFs and finds generated score files.
- **Job orchestration/API:** owns uploads, job state, progress, errors, result
  retention, HTTP status codes, and artifact packaging.

Do not bury parsing logic in endpoint handlers. Do not make the pure converter
depend on global FastAPI job state.

## Testing expectations

Before making changes, locate the real test commands and existing fixtures.
Add focused fixtures instead of relying only on a large Für Elise conversion.

At minimum, preserve or add coverage for:

1. Plain `score-partwise` XML.
2. Valid compressed MXL with `META-INF/container.xml`.
3. Malformed XML and invalid MXL.
4. Rejection of `score-timewise`.
5. Empty-part rejection.
6. Multiple parts plus `MULTIPLE_PARTS_IGNORED`.
7. Notes, rests, chords, dots, accidentals, keys, times, and per-staff clefs.
8. Multiple voices/staves using `backup` and `forward`.
9. Accurate event start and quarter-note duration values in 4/4 and 3/8.
10. Source stems and multi-level beam markers.
11. Tie and slur start/stop pairing.
12. Left/right barlines, repeats, endings, and final light-heavy barline.
13. Deterministic normalization records.
14. Deterministic difficulty output.
15. FastAPI upload field, job status progression, result status codes, and
    direct XML/MXL bypass of Audiveris.

Use semantic assertions on parsed JSON. Avoid snapshot-only tests that make it
hard to identify which musical invariant failed.

## Required working procedure

Before editing:

1. Run `git status` and identify the baseline commit.
2. Read all applicable repository and nested `AGENTS.md` files.
3. Locate `main.py`, `musicxml_converter.py`, the MXL decompressor, tests,
   fixtures, Dockerfile, dependency files, and deployment documentation.
4. Identify the public converter functions called by FastAPI.
5. Trace one XML input and one MXL input through upload, validation,
   conversion, normalization, artifact writing, and result response.
6. Confirm the emitted schema version and compare it with TypeScript consumer
   types if those types are available.
7. Explain the existing behavior and smallest safe change before editing.

While implementing:

1. Keep parsing/normalization deterministic and testable without network access
   or Audiveris.
2. Preserve stable IDs and written-order source semantics.
3. Add warnings or normalization records for lossy/uncertain decisions.
4. Avoid broad formatting or unrelated refactors.
5. Do not change API status codes, response field names, or upload semantics
   incidentally.

Before finishing:

1. Run the relevant Python tests and FastAPI tests.
2. Run formatting, linting, and static/type checks configured by the repository.
3. Validate representative XML and MXL fixtures.
4. Inspect the complete Git diff for schema or API changes.
5. If Docker is available, build the current image and confirm the converter
   version/schema in its output or health/debug metadata.
6. Report every modified file, command run, result, warning, schema impact, API
   impact, and remaining limitation.
7. Do not claim full success if Audiveris, Docker, or integration validation
   could not run; state the blocker precisely.

## First Codex prompt

Place this file at the converter repository root as `AGENTS.md`, start Codex
from that directory, and use:

> Read `AGENTS.md`, then inspect the current repository and Git history without
> editing. Identify the authoritative converter/schema version, trace XML, MXL,
> and PDF inputs through the FastAPI job workflow, and compare the implementation
> with the schema 1.2 source-notation contract described here. Report any
> mismatch, stale snapshot, untested behavior, or API compatibility risk. Then
> propose the smallest prioritized stabilization plan and wait for my approval
> before modifying files.
