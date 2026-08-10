"""Negative contract: the ticket console has no file-interchange substrate.

The product is intentionally an in-platform evaluation ledger.  Keeping dormant
CSV/import/export models or repository methods would leave a reusable ingestion
or exfiltration surface even when no HTTP route exposes it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from api.ticket_review_models import ImportState, ReviewPatch, TicketReview


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _defined_symbols(relative_path: str) -> set[str]:
    tree = ast.parse((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
    return symbols


def test_api_has_no_ticket_file_interchange_symbols() -> None:
    forbidden_by_module = {
        "api/ticket_review_models.py": {
            "ImportStatus",
            "TicketImport",
            "TicketImportRow",
            "ImportDryRunResponse",
            "ImportChunkResponse",
            "ImportApplyOrReverseRequest",
            "ImportRowIssue",
            "InvalidImportTransition",
            "MAX_CSV_ROWS",
            "MAX_CSV_REQUEST_BYTES",
            "IMPORT_STAGING_TTL_S",
            "_IMPORT_TRANSITIONS",
            "allowed_import_transitions",
            "assert_import_transition",
        },
        "api/tickets_console_config.py": {
            "MAX_CSV_BYTES",
            "MAX_CSV_ROWS",
            "IMPORT_STAGING_TTL_S",
        },
        "api/tickets_csrf.py": {"CSV_CONTENT_TYPE"},
    }

    for relative_path, forbidden in forbidden_by_module.items():
        present = forbidden & _defined_symbols(relative_path)
        assert not present, f"{relative_path} retains file interchange symbols: {sorted(present)}"


def test_repository_has_no_ticket_file_interchange_surface() -> None:
    forbidden = {
        "TicketExportSummary",
        "ImportRowSpec",
        "IMPORTS_COLLECTION",
        "IMPORT_ROWS_SUBCOLLECTION",
        "IMPORT_STAGING_COLLECTION",
        "EXPORTS_COLLECTION",
        "IMPORT_ROWS_CURSOR_CONTEXT",
        "create_import",
        "get_import",
        "patch_import",
        "stage_import_rows",
        "get_staged_import_rows",
        "_write_import_rows",
        "apply_import_rows",
        "reverse_import_rows",
        "list_import_rows",
        "create_export",
            "get_export",
            "mark_import_state",
        }
    present = forbidden & _defined_symbols("data_pipeline/ticket_review_repository.py")
    assert not present, f"repository retains file interchange surface: {sorted(present)}"


def test_storage_declarations_have_no_ticket_file_interchange_collections() -> None:
    forbidden_collection_ids = {
        "ticket_imports",
        "ticket_import_staging",
        "ticket_exports",
    }
    sources = [PROJECT_ROOT / "firestore.indexes.json"]
    sources.extend((PROJECT_ROOT.parent / "infra" / "terraform").rglob("*.tf"))

    offenders: list[str] = []
    for source in sources:
        text = source.read_text(encoding="utf-8")
        found = sorted(value for value in forbidden_collection_ids if value in text)
        if found:
            offenders.append(f"{source}: {', '.join(found)}")
    assert not offenders, "file interchange collections remain declared:\n" + "\n".join(offenders)


def test_historical_import_metadata_is_read_only_compatibility() -> None:
    assert "import_state" in TicketReview.model_fields
    assert "legacy_reviewer_display_name" in TicketReview.model_fields
    assert ImportState.REVERSED.value == "reversed"
    assert "legacy_reviewer_display_name" not in ReviewPatch.model_fields

    evaluation = (PROJECT_ROOT / "ui/tickets/assets/evaluation.js").read_text(encoding="utf-8")
    document = (PROJECT_ROOT / "ui/tickets/index.html").read_text(encoding="utf-8")
    assert "eval-legacy-reviewer" not in evaluation
    assert "eval-legacy-reviewer" not in document
