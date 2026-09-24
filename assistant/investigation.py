"""Keep the selected investigation's source accessible without inventing links."""
import json
import re


class InvestigationEvidence:
    def __init__(self, question: str):
        # Deliberately narrow: the UI's explicit single-property action only.
        match = re.match(r"Investigate only Apex property ID ([1-9]\d*)\b", question)
        self.property_id = int(match[1]) if match else None
        self.verified = False

    def fresh_messages(self, messages):
        """The explicit ID action can recheck records without recycling old analysis.

        Preserve every user requirement verbatim. Ordinary conversation keeps
        its full history; only this narrowly identified investigation uses this
        path. A reply such as 'yes' may need clarification without its question.
        """
        if self.property_id is None:
            return messages
        return [dict(turn) if turn.get("role") == "user" else {
            "role": "assistant",
            "content": "[Earlier answer omitted. This earlier request was already answered; retain user-stated requirements as context, not outstanding tasks.]",
        } for turn in messages if turn.get("role") in ("user", "assistant")]

    def answer_guidance(self):
        if self.property_id is None:
            return ""
        return """
FRESH SELECTED-PROPERTY INVESTIGATION
Answer ONLY the final user request about the selected property. Earlier requests
have already been answered: they supply constraints, not a queue of tasks.
Do not recap the shortlist, redraw earlier charts or mention repeated requests.
Previous assistant analysis is deliberately absent. Preserve requirements in
the user turns, with later explicit changes taking precedence. If a reply like
'yes', 'that area' or 'the cheaper one' cannot be resolved from those user turns,
ask for the missing requirement; do not invent the omitted context.
Fetch the selected property's current detail and relevant sold evidence. Write
a fresh brief, not a reconstruction of a previous answer. Use this structure:
1. Two-sentence assessment with the verified property link and biggest unknown.
2. One compact facts/estimates table, stating each monetary figure once.
3. At most three sold examples with dates and measured similarities/limitations.
4. One priority next check and what remains unresolved.
Target 250-350 words. The number of examples shown is not the valuation sample.
Do not claim sale method causes a price difference or determines this home's
value. Model confidence is a label, not demonstrated accuracy. No single check
proves suitability. Do not claim all records agree after checking only one.
"""

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
