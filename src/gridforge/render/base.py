"""Map renderer protocol interface."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from gridforge.config import JobWorkspace, RenderResult


@runtime_checkable
class MapRenderer(Protocol):
    """Protocol for map rendering backends consuming a normalized JobWorkspace."""

    def render_pdf(self, workspace: JobWorkspace, output_pdf: Path) -> RenderResult:
        """Render a prepared workspace into a print-ready topographic PDF."""
        ...
