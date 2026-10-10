# Distribute 2x CODA registration across eight Windows computers

The workflow assigns each eligible slide to exactly one computer. Each computer
uses its own MATLAB licence, reads the same tissue root, and writes transforms
only inside its assigned slide folders. The CODA registration algorithm itself
is unchanged.

Requirements on **each** computer:

1. MATLAB with Image Processing Toolbox and permission to read/write the shared
   tissue root.
2. A clone of this repository at the same revision on all eight computers.
3. The external CODA MATLAB source installed locally:

   ```powershell
   python tools/setup_coda.py --source "C:\path\to\CODA\update 12-13-2023"
   ```

CODA is maintained separately and is not redistributed in this repository.
See [the main user guide](../docs/USER_GUIDE.md) for its provenance.

## 1. Stop a previous unsharded batch

Do not run the eight workers alongside an older `run_registration_batch(root,'',true)`
job: that job can select the same slides. A slide interrupted in the middle of
registration retains partial `registered`/`TA` directories. This workflow
excludes it until those outputs are reviewed and archived.

## 2. Create one shared assignment snapshot

On the coordinator, from `02_calculate_registration`:

```powershell
$tissueRoot = '\\server\share\Scanned images\Tissue types'
.\prepare_shards.ps1 -Root $tissueRoot
```

This creates `manifests/assignment.csv`, `excluded.csv`, and `worker_01.csv`
through `worker_08.csv`. The planner selects only folders with exactly one TIFF
for each of S210, S360, Leica, Olympus, P1000, Pramana, Roche, and Zeiss, and
without registration or mask outputs. It balances the workers using total TIFF
bytes as a rough cost estimate. MATLAB performs a full input and MPP check
before each registration. Review `excluded.csv` for missing or partial cases.

Make the **same** `manifests` directory available to all eight computers,
either by copying it into each clone or by placing it in one read-only shared
location and passing `-ManifestDirectory`. Do not regenerate a separate
assignment on each computer or edit the lists after starting a worker.
Research manifests and logs are ignored by Git; share them privately rather
than committing tissue identifiers.

## 3. Start one worker per computer

Use a different ID on each machine:

```powershell
.\launch_worker.ps1 -WorkerId 1 -Root $tissueRoot
# For a shared manifest directory:
.\launch_worker.ps1 -WorkerId 2 -Root $tissueRoot -ManifestDirectory '\\server\share\shards'
# On the other seven computers, use IDs 2 through 8.
```

The launcher writes a local timestamped progress log and error log. Follow the
log with `Get-Content .\worker_01_*.log -Tail 30 -Wait`. On completion, the
worker writes `worker_01_results.csv`; the CODA wrapper also writes uniquely
named audit files under the shared root's `batch_registration_logs` folder.
Existing outputs are skipped when a worker is relaunched after interruption.

## Partial output recovery

For one confirmed incomplete slide, with every process for that slide stopped:

```powershell
.\archive_partial.ps1 -SlideId 'Example slide' -Root $tissueRoot
.\archive_partial.ps1 -SlideId 'Example slide' -Root $tissueRoot -Apply
```

The first command is a preview. `-Apply` moves the partial `registered` and
`TA` directories into a timestamped archive inside the slide's `2x` folder;
it does not remove the TIFFs. Replan the recovered slide only after existing
worker assignments are finished, so it cannot be assigned twice. This helper
refuses to archive a slide with all expected warp MAT files present.

`computed_pending_visual_qc` means the registration calculation finished; it
does not approve anatomical accuracy. Inspect overlays and selected 40x crops
before downstream use. With eight workers, the NAS may become the throughput
bottleneck; compare completed slides per hour after the first few finish.
