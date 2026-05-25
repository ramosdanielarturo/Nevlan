"""
Typed error codes for OCR collection resolution at runtime.
"""
from __future__ import annotations

COLLECTION_ITEM_NOT_FOUND = "COLLECTION_ITEM_NOT_FOUND"
COLLECTION_AMBIGUOUS = "COLLECTION_AMBIGUOUS"
OCR_DETECTOR_UNAVAILABLE = "OCR_DETECTOR_UNAVAILABLE"

COLLECTION_ERROR_CODES: frozenset = frozenset({
    COLLECTION_ITEM_NOT_FOUND,
    COLLECTION_AMBIGUOUS,
    OCR_DETECTOR_UNAVAILABLE,
})

REPAIR_SUGGESTIONS: dict = {
    COLLECTION_ITEM_NOT_FOUND: (
        "Re-graba el click sobre el elemento visible o confirma el "
        "texto exacto en Mission Review."
    ),
    COLLECTION_AMBIGUOUS: (
        "Varios elementos coinciden con el texto. Re-graba un click "
        "más preciso o edita label_text en el plan semántico."
    ),
    OCR_DETECTOR_UNAVAILABLE: (
        "Instala el extra opcional vision/desktop y el binario OCR "
        "(ver README § OCR opcional)."
    ),
}
