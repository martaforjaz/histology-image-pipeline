"""Append per-image wall-clock measurements without changing image processing."""

import csv
import platform
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    'run_id', 'started_utc', 'computer', 'scanner', 'image', 'source',
    'pipeline', 'phase', 'resolution', 'mpp', 'status', 'seconds', 'detail',
]

_settings = ContextVar('timing_settings', default=('unknown', None))


def image_id(value):
    name = Path(value).name
    for suffix in (
        '.ome.tiff', '.ome.tif', '.tiff', '.tif', '.vsi', '.czi', '.svs',
        '.ndpi', '.ndp', '.scn', '.mrxs', '.dcm', '.qptiff',
        '.isyntax', '.i2syntax',
    ):
        if name.lower().endswith(suffix):
            return name[:-len(suffix)]
    return name


@contextmanager
def timing_settings(scanner='unknown', scanner_manifest=None):
    token = _settings.set((scanner or 'unknown', scanner_manifest))
    try:
        yield
    finally:
        _settings.reset(token)


def active_scanner():
    """Scanner selected for the active conversion batch."""
    return _settings.get()[0]


class TimingLog:
    def __init__(self, output, pipeline):
        self.scanner, manifest = _settings.get()
        self.scanners = {}

        if manifest:
            with open(manifest, newline='', encoding='utf-8-sig') as stream:
                reader = csv.DictReader(stream)

                if not {'image', 'scanner'} <= set(reader.fieldnames or []):
                    raise ValueError(
                        'Scanner manifest requires image,scanner columns.'
                    )

                for row in reader:
                    key = image_id(row['image'])
                    scanner = row['scanner'].strip()

                    if not key or not scanner or key in self.scanners:
                        raise ValueError(
                            'Scanner manifest needs unique image IDs '
                            'and nonempty scanners.'
                        )

                    self.scanners[key] = scanner

        self.run_id = str(uuid.uuid4())
        self.pipeline = pipeline
        self.path = (
            Path(output) / 'timings'
            / f'{pipeline}_{self.run_id}.csv'
        )

        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self.path.open('x', newline='', encoding='utf-8') as stream:
            csv.DictWriter(stream, fieldnames=FIELDS).writeheader()

        print(f'Timing log: {self.path}', flush=True)

    def _append_row(self, row):
        for attempt in range(10):
            try:
                with self.path.open(
                    'a', newline='', encoding='utf-8'
                ) as stream:
                    csv.DictWriter(
                        stream, fieldnames=FIELDS
                    ).writerow(row)
                return

            except PermissionError:
                if attempt == 9:
                    raise

                print(
                    'CSV temporariamente inacessível; '
                    'nova tentativa em 2 segundos...',
                    flush=True,
                )
                time.sleep(2)

    @contextmanager
    def measure(
        self, source='', phase='image_total',
        resolution='', mpp='', status='ok',
    ):
        key = image_id(source) if source else ''

        row = dict(
            run_id=self.run_id,
            started_utc=datetime.now(timezone.utc).isoformat(),
            computer=platform.node(),
            scanner=self.scanners.get(key, self.scanner),
            image=key,
            source=str(source),
            pipeline=self.pipeline,
            phase=phase,
            resolution=resolution,
            mpp=mpp,
            status=status,
            detail='',
        )

        start = time.perf_counter()

        try:
            yield row
        except BaseException as exc:
            row.update(
                status='error',
                detail=f'{type(exc).__name__}: {exc}',
            )
            raise
        finally:
            row['seconds'] = f'{time.perf_counter() - start:.6f}'
            self._append_row(row)

            if phase == 'image_total':
                print(
                    f"  Time for {key}: "
                    f"{row['seconds']} s ({row['status']})",
                    flush=True,
                )
