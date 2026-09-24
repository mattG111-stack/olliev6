"""Keep the selected investigation's source accessible without inventing links."""
import json
import re


class InvestigationEvidence:
    def __init__(self, question: str):
        # Deliberately narrow: the UI's explicit single-property action only.
        match = re.match(r"Investigate only Apex property ID ([1-9]\d*)\b", question)
        self.property_id = int(match[1]) if match else None
        self.verified = False

    def wrap(self, dispatch):
        def run(name, args):
            result = dispatch(name, args)
            if self.property_id is not None and name == "get_property":
                try:
                    record = json.loads(result)
                    self.verified = self.verified or (
                        isinstance(record, dict)
                        and type(record.get("id")) is int
                        and record["id"] == self.property_id
                        and record.get("apex_url") == f"/property/{self.property_id}"
                    )
                except (ValueError, TypeError):
                    pass
            return result
        return run

    def link_answer(self, answer: str) -> str:
        if not self.verified or not answer.strip():
            return answer
        url = f"/property/{self.property_id}"
        if re.search(r"\]\(" + re.escape(url) + r"\)", answer):
            return answer
        # Static label avoids interpreting imported address text as Markdown.
        return answer.rstrip() + f"\n\n[View the checked property record]({url})"
