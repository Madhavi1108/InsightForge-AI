"""Phase 1 - verify the architecture / requirements / design docs and the
consolidated 36-phase roadmap are in place."""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCS = PROJECT_ROOT / "docs"

PHASE1_DOCS = [
    "architecture.md",
    "system-components.md",
    "data-flow.md",
    "security.md",
    "requirements.md",
    "business-questions.md",
    "dataset-design.md",
]

REQUIRED_HEADINGS = {
    "architecture.md": ["Layered architecture", "Failure paths", "Control flow"],
    "system-components.md": ["Ingestion", "Storage", "AI"],
    "data-flow.md": ["Directory lifecycle", "Stage-by-stage flow", "immutab"],
    "security.md": ["Secrets", "NL-to-SQL", "Auditability"],
    "requirements.md": ["Functional requirements", "Non-functional requirements"],
    "business-questions.md": ["Core questions", "Mapping rule"],
    "dataset-design.md": ["Field dictionary", "Derived-field rules", "Geography"],
}

DATASET_FIELDS = [
    "Order_ID", "Order_Date", "Customer_ID", "Customer_Name", "Customer_Segment",
    "Product_ID", "Product_Name", "Category", "Sub_Category", "Region", "State",
    "City", "Quantity", "Unit_Price", "Discount", "Revenue", "Cost", "Profit",
    "Payment_Method", "Shipping_Days", "Order_Status", "Return_Status",
]


def test_phase1_docs_exist_and_are_non_empty():
    missing = [d for d in PHASE1_DOCS if not (DOCS / d).is_file()]
    assert not missing, f"missing docs: {missing}"
    empty = [d for d in PHASE1_DOCS if (DOCS / d).stat().st_size < 200]
    assert not empty, f"docs too small to be real content: {empty}"


def test_phase1_docs_contain_required_sections():
    for name, needles in REQUIRED_HEADINGS.items():
        text = (DOCS / name).read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, f"{name} is missing a section mentioning {needle!r}"


def test_dataset_design_documents_every_field():
    text = (DOCS / "dataset-design.md").read_text(encoding="utf-8")
    missing = [f for f in DATASET_FIELDS if f not in text]
    assert not missing, f"dataset-design.md does not document: {missing}"
    assert len(DATASET_FIELDS) == 22


def test_phase_map_exists_and_covers_all_69_spec_phases():
    text = (DOCS / "PHASE_MAP.md").read_text(encoding="utf-8")
    covered: set[int] = set()
    # rows look like: | 1  | 1-4   | ... |  or  | 0  | 0     | ...
    for spec in re.findall(r"\|\s*\d+\s*\|\s*([0-9]+(?:-[0-9]+)?)\s*\|", text):
        if "-" in spec:
            lo, hi = (int(x) for x in spec.split("-"))
            covered.update(range(lo, hi + 1))
        else:
            covered.add(int(spec))
    missing = sorted(set(range(0, 70)) - covered)
    assert not missing, f"PHASE_MAP.md does not map spec phases: {missing}"


def test_phase_status_has_36_build_phases_and_no_old_numbering():
    text = (DOCS / "PHASE_STATUS.md").read_text(encoding="utf-8")
    phase_numbers = sorted(
        int(m) for m in re.findall(r"^\|\s*(\d+)\s*\|", text, flags=re.MULTILINE)
    )
    assert phase_numbers == list(range(0, 37)), phase_numbers
    for stale in ("| 37 |", "| 50 |", "| 69 |"):
        assert stale not in text, f"stale 69-phase numbering left in PHASE_STATUS.md: {stale}"


def test_phase_status_marks_phase_1_complete():
    text = (DOCS / "PHASE_STATUS.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if re.match(r"\|\s*1\s*\|", line))
    assert "COMPLETE" in row
