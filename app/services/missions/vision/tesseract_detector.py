"""
Optional Tesseract backend — only loaded when pytesseract is installed.

Install: ``pip install -e ".[vision]"`` and ensure tesseract binary is on PATH.
"""
from __future__ import annotations

from typing import Dict, List

from app.services.missions.vision.text_detector import TextDetection


class TesseractDetector:
    """Full-screen OCR via pytesseract (lazy, desktop-only)."""

    def detect(self, image_path: str) -> List[TextDetection]:
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except Exception:
            return []
        try:
            with Image.open(image_path) as im:
                data = pytesseract.image_to_data(
                    im, output_type=pytesseract.Output.DICT,
                )
        except Exception:
            return []

        texts = data.get("text") or []
        lefts = data.get("left") or []
        tops = data.get("top") or []
        widths = data.get("width") or []
        heights = data.get("height") or []
        confs = data.get("conf") or []

        # Group words into lines by block_num + line_num
        line_map: Dict[tuple, List[int]] = {}
        for i, txt in enumerate(texts):
            ws = str(txt or "").strip()
            if not ws:
                continue
            key = (
                int(data.get("block_num", [0] * len(texts))[i] or 0),
                int(data.get("line_num", [0] * len(texts))[i] or 0),
            )
            line_map.setdefault(key, []).append(i)

        out: List[TextDetection] = []
        for indices in line_map.values():
            words = [str(texts[j] or "").strip() for j in indices]
            text = " ".join(w for w in words if w)
            if not text:
                continue
            xs = [int(lefts[j]) for j in indices if j < len(lefts)]
            ys = [int(tops[j]) for j in indices if j < len(tops)]
            ws = [int(widths[j]) for j in indices if j < len(widths)]
            hs = [int(heights[j]) for j in indices if j < len(heights)]
            if not xs:
                continue
            left = min(xs)
            top = min(ys)
            right = max(x + w for x, w in zip(xs, ws))
            bottom = max(y + h for y, h in zip(ys, hs))
            conf_vals = [
                float(confs[j]) for j in indices
                if j < len(confs) and str(confs[j]).replace(".", "").isdigit()
            ]
            conf = (sum(conf_vals) / len(conf_vals) / 100.0) if conf_vals else 0.5
            out.append(TextDetection(
                text=text,
                bbox={
                    "left": left,
                    "top": top,
                    "width": max(1, right - left),
                    "height": max(1, bottom - top),
                },
                confidence=min(1.0, max(0.0, conf)),
            ))
        return out
