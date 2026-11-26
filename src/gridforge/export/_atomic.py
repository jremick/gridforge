"""Publish a completed single-file export on the destination filesystem."""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)


@contextmanager
def staged_archive(output: Path, *, sidecar_suffixes: Sequence[str] = ()) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix=".gridforge-", dir=output.parent))
    staged = directory / output.name
    try:
        yield staged
        # A journal or metadata file belongs to the previous database.
        for suffix in sidecar_suffixes:
            sidecar = Path(f"{output}{suffix}")
            if sidecar.exists():
                raise FileExistsError(
                    f"Cannot replace '{output}' while '{sidecar.name}' exists. "
                    "Close the archive and choose a fresh output name if the sidecar remains."
                )
        staged.replace(output)
    finally:
        try:
            shutil.rmtree(directory)
        except OSError:
            logger.warning("Could not remove export staging directory %s", directory, exc_info=True)
