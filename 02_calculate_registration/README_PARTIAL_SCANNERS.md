# S210-anchored registration with 4–7 scanner TIFFs

Use this workflow for physical slides that do not have all eight scanner TIFFs
but do have at least four known scanner TIFFs, including `<slide>_S210.tif`.
The S210 image is selected explicitly as CODA's reference for every slide.
The final canvas is anchored to S210; CODA may register adjacent moving images
sequentially internally. Confirm the final overlays before downstream use.

The eight `Hum Liv PDAC` immunohistochemistry folders named CD1A, CD20,
CD68, CD163, CK8, Collagen IV, Ki-67, and vimentin run with CODA `IHC=1`.
All other folders run with `IHC=0` (H&E). The external CODA dependency must
be installed locally as described in [the user guide](../docs/USER_GUIDE.md).

## Prepare a fixed campaign

Stop any unsharded registration process that could select these same slides.
From `02_calculate_registration` on one coordinator:

```powershell
$tissueRoot = '\\server\share\Scanned images\Tissue types'
.\prepare_partial_shards.ps1 -Root $tissueRoot
```

The planner writes `manifests_partial/assignment.csv`, `excluded.csv`, and
eight `worker_XX.csv` files. It selects only 4–7 expected TIFFs with S210,
without partial or existing outputs, and balances total TIFF bytes. Review
`excluded.csv`, particularly `missing_S210_anchor` and
`existing_or_partial_output`. Freeze this manifest snapshot; do not let each
computer make its own assignment. Share the one directory privately by copying
it to each clone or pointing all workers at one NAS location. Manifests are
ignored by Git because they contain research slide identifiers.

## Execute

Use a different worker ID on each computer. Each needs MATLAB with Image
Processing Toolbox, the CODA source, and NAS read/write access:

```powershell
.\launch_partial_worker.ps1 -WorkerId 1 -Root $tissueRoot
# If the frozen lists are in a shared location:
.\launch_partial_worker.ps1 -WorkerId 2 -Root $tissueRoot -ManifestDirectory '\\server\share\partial_shards'
```

The MATLAB wrapper fully decodes the TIFFs, checks RGB and 5 µm/pixel in both
axes, confirms the inputs stayed unchanged, and checks every expected global
and elastic warp before recording `computed_pending_visual_qc`. Existing or
partial outputs are never overwritten. Local logs are named
`partial_worker_XX_*.log`, and the NAS audit filenames are unique across PCs.
The same input TIFFs must not be assigned in the eight-scanner campaign.

If a computer is still running an eight-scanner worker, use
`queue_partial_worker.ps1` with the same arguments. It waits for local MATLAB
processes to exit, then starts the partial worker; the default wait limit is
24 hours. Do not start a second worker with the same ID elsewhere.

Slides lacking an S210 TIFF cannot be registered under this S210-anchor rule.
Do not silently substitute another scanner as the reference. A slide left
partial after interruption must be reviewed and archived before replanning;
see [the eight-worker guide](README_8_WORKERS.md#partial-output-recovery).
