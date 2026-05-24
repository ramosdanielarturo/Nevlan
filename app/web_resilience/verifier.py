from typing import Any
import re

class WebVerifier:
    def verify_url_contains(self, current_url: str, substring: str) -> bool:
        return substring in current_url

    def verify_element_visible(self, element: Any) -> bool:
        try:
            return element.is_visible()
        except:
            return False

    def verify_text_present(self, page_content: str, text: str) -> bool:
        return text in page_content

    def verify_title_contains(self, title: str, substring: str) -> bool:
        return substring in title
