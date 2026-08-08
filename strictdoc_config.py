"""StrictDoc project config (research 002 Phase 1 item 2).

@relation(UID, scope=function) markers in wingman/ docstrings bind requirements
to function nodes; the export fails with exit 1 when a marker names a UID that
does not exist, or an sdoc relation dangles. Paths are relative to the project
root, which is the strictdoc invocation input path — so `make reqs-gate` runs
strictdoc against the repo root (.), with document discovery scoped to
docs/requirements only.

Pinned to strictdoc==0.27.1 (0.x tool: do not float — research 002 risk table).
"""

from strictdoc.core.project_config import ProjectConfig


def create_config() -> ProjectConfig:
    return ProjectConfig(
        project_title="MetalStorm Wingman Requirements",
        project_features=[
            "REQUIREMENT_TO_SOURCE_TRACEABILITY",
        ],
        include_doc_paths=[
            # .sdoc only: the committed markdown exports live beside the
            # sources, and StrictDoc 0.27 ingests .md as documents — matching
            # them re-declares the document UIDs and fails the build.
            "docs/requirements/*.sdoc",
        ],
        include_source_paths=[
            "wingman/*.py",
        ],
    )
