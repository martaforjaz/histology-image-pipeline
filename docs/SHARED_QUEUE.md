# Shared 2x registration queue (Windows)

Use this runner when several Windows computers should process independent 2x tissue
slides on the same SMB/NAS share. It adds a coordinator around the existing CODA
registration; it does not change `run_registration.m` or
`run_registration_batch.m`.

## Before starting

1. Clone the same repository revision on every computer. Install MATLAB with
   Image Processing Toolbox and set up the external CODA code as described in
   [USER_GUIDE.md](USER_GUIDE.md).
2. Give every computer read/write access to the **same UNC tissue root**. Every
   worker must pass that exact root path. Do not use different copies or mapped
   drives pointing to different shares.
3. Stop any older `run_registration_batch` process before starting queue
   workers. That older process does not take queue claims and could race.
4. Inspect existing partial outputs. The queue runner does not overwrite a
   `registered` or `TA` directory, including interrupted outputs.

From PowerShell on **each** computer:

```powershell
git clone https://github.com/martaforjaz/histology-image-pipeline.git
cd histology-image-pipeline\02_calculate_registration
$root = '\\10.17.182.53\kiemen-lab-data\Marta Pereira\Multi scanner project\Scanned images\Tissue types'
& 'C:\Program Files\MATLAB\R2024b\bin\matlab.exe' -batch "run_registration_shared_queue('$root', true)"
& 'C:\Program Files\MATLAB\R2024b\bin\matlab.exe' -batch "run_registration_shared_queue('$root')"
```

The first call is a read-only eligibility listing. Run the second call on up to
eight computers. Keep each MATLAB terminal open; it prints claims and per-slide
progress. Different slide durations are balanced naturally: after finishing one
slide, a worker attempts the next available slide.

## Claims and results

The runner creates
`<tissue root>/batch_registration_queue/claims/<slide ID>/` using an atomic
directory-create operation on the SMB server. Only the worker that creates it
runs that slide. Each claim holds `status.csv` (worker, UTC timestamps, status,
error detail) and `scanners.csv`. Claims are never removed automatically.

`computed_pending_visual_qc` means transforms were calculated and the expected
MAT files exist; it does **not** mean visual QC passed. `failed_review_required`
or a claim with only a `running` status may have partial CODA outputs. Preserve
those outputs for investigation. After diagnosing a failure, a human can decide
whether to use a separate working copy or archive the partial outputs and
explicitly release that slide's claim. Never release a claim while any worker
may still be processing that slide.

The runner requires exactly one 2x TIFF for each of S210, S360, Leica, Olympus,
P1000, Pramana, Roche, and Zeiss; checks 5 µm/pixel and RGB readability; uses
S210 as the anchor; and leaves existing results untouched. Incomplete slides
and excluded IHC folders are skipped. Original CODA timing files remain inside
each slide's `2x/timings` directory.

This is a shared-filesystem queue, not an unattended recovery service. If the
NAS disconnects, the worker terminates or records failure; inspect the claim
and output directories before retrying.
