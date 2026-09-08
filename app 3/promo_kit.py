"""What a promoter is given to promote WITH: the facts, the rules, and ad copy.

Two halves, and the split matters.

The **media pack** is fixed. Brand colours, what the product is, what may and may
not be claimed. It is the same for everyone and it is written down here rather
than left to each influencer's imagination, because the alternative is finding
out from a customer that someone promised guaranteed returns on a property site.

The **ad copy** is generated, and it is generated against those same rules. The
model is given the facts and the prohibitions in its system prompt, and every
draft still goes to the promoter as a draft. Nothing here posts anything.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

from .assistant import providers
from .models import AssistantLog, Promoter

log = logging.getLogger(__name__)

# What the product actually is, in the words a promoter may use. Kept here so
# every generated ad and every template says the same thing, and so correcting a
# claim is one edit rather than a message to twelve influencers.
PRODUCT = {
    "name": "Apex Property",
    "what": "A property research tool for Auckland. It prices every listing against "
            "recent comparable sales, so you can see which ones are asking under "
            "what the data says they are worth, and which sections have room to "
            "subdivide.",
    "who": "Investors, first-home buyers doing their own research, and anyone who "
           "wants a second opinion on an asking price before they bid.",
    "points": [
        "Every Auckland listing priced against recent comparable sales",
        "Underpriced listings surfaced, ranked by the gap to fair value",
        "Subdivision potential worked out per section, with the numbers behind it",
        "Suburb trends: what is selling, at what price, and how fast",
        "Ask Ollie — plain-English questions about any suburb or listing",
        "7-day free trial, card required, cancel any time before it ends",
    ],
}

# The lines that must not be crossed. These are in the system prompt for every
# generated ad AND printed in the promoter's media pack, because a rule only an
# AI can see does not stop a human writing their own caption.
RULES = [
    "Never promise or imply a guaranteed profit, return, or capital gain.",
    "Never call it financial or investment advice. It is research data.",
    "Never invent statistics, customer numbers, or testimonials.",
    "Never claim it predicts the market or knows what a property will sell for.",
    "Never use another company's name, branding, or listings to imply a partnership.",
    "Always make it clear the trial needs a card and renews unless cancelled.",
    "Disclose that the link is a paid referral — most platforms require it, and "
    "on Instagram and TikTok that means the paid-partnership label.",
]

COLOURS = [
    {"name": "Apex red", "hex": "#E4002B", "use": "the mark, and nothing else — never as a background for text"},
    {"name": "Ink", "hex": "#0E1B2E", "use": "headlines and body text on light backgrounds"},
    {"name": "Paper", "hex": "#F4F7FB", "use": "page background"},
    {"name": "Blue", "hex": "#1F6FEB", "use": "links and buttons"},
    {"name": "Green", "hex": "#0A8754", "use": "a good number — under value, a gain"},
]

# Curated copy that works without a model behind it. An influencer opening this
# page at 11pm should not be blocked by an API key an admin has not set yet.
TEMPLATES = [
    {
        "channel": "Instagram / TikTok caption",
        "text": "I stopped guessing what Auckland houses are actually worth.\n\n"
                "{name} prices every listing against what similar places actually "
                "SOLD for — so you can see the ones asking under the data before "
                "everyone else does. It also flags which sections can be "
                "subdivided.\n\n"
                "7-day free trial on my link — it takes a card and renews unless "
                "you cancel before day 7: {link}\n\n"
                "#ad #paidpartnership — property data, not financial advice.",
    },
    {
        "channel": "Short video script (30s)",
        "text": "[0-3s] \"This house is listed at $1.2m. Here's what it's actually worth.\"\n"
                "[3-10s] Screen recording: open the listing in {name}, show the fair "
                "value and the gap to the asking price.\n"
                "[10-20s] \"It works this out from recent sales of similar places in "
                "the same suburb. Every Auckland listing, every week.\"\n"
                "[20-27s] Show the subdivision panel on a section.\n"
                "[27-30s] \"Free for 7 days — card required, cancel any time before "
                "then. Link in bio.\" — {link}\n\n"
                "Say it is a paid partnership in the first three seconds.",
    },
    {
        "channel": "Facebook / LinkedIn post",
        "text": "If you are buying in Auckland, the hardest question is not which "
                "house — it is whether the asking price is fair.\n\n"
                "{name} answers that with data: every listing priced against recent "
                "comparable sales, the underpriced ones ranked, and the sections "
                "with subdivision potential worked out with the numbers shown.\n\n"
                "There is a 7-day free trial (card required, cancel any time). "
                "This is my referral link and I earn from it: {link}\n\n"
                "Research data, not financial advice.",
    },
    {
        "channel": "Newsletter / email",
        "text": "Subject: The tool I use to check an Auckland asking price\n\n"
                "Every listing has a number on it, and almost none of them come with "
                "a reason. {name} gives you the reason: it prices each Auckland "
                "listing against recent comparable sales and shows you the gap, plus "
                "which sections have room to subdivide and what that would cost.\n\n"
                "You get 7 days free to try it — a card is needed and it renews "
                "unless you cancel before day 7.\n\n"
                "{link}\n\n"
                "(Referral link — I earn a commission if you subscribe. It is "
                "property research data, not financial advice.)",
    },
    {
        "channel": "YouTube description",
        "text": "Tool used in this video: {name} — {link}\n"
                "Auckland listings priced against recent comparable sales, with "
                "subdivision potential per section. 7-day free trial, card required, "
                "cancel any time.\n\n"
                "This is a paid referral link. Property research data, not financial "
                "advice. Do your own due diligence.",
    },
]


def templates_for(link: str) -> list[dict]:
    return [{"channel": t["channel"],
             "text": t["text"].format(name=PRODUCT["name"], link=link)}
            for t in TEMPLATES]


def media_pack(link: str) -> dict:
    return {
        "product": PRODUCT,
        "rules": RULES,
        "colours": COLOURS,
        "templates": templates_for(link),
    }


# ---- generated ads ----------------------------------------------------------

# Ad drafting is metered separately from Ask Ollie and much more tightly. It is
# a nice-to-have on somebody else's key, and one promoter with an idea and a
# free evening should not be able to spend the account's whole allowance.
DAILY_ADS = 15

SYSTEM = """You write short advertising copy for an affiliate promoting a product.

THE PRODUCT
{name}: {what}
Who it is for: {who}
What it does:
{points}

HARD RULES — breaking any of these makes the copy unusable:
{rules}

HOW TO WRITE
Write the way the requested channel actually reads. A TikTok caption is not a
LinkedIn post with fewer words. Be specific and concrete — a real number from
the product's own screen beats an adjective. No exclamation marks stacked up, no
"game changer", no "unlock". Do not invent figures: you may describe what the
product shows, never what it found for a particular house.

Include the referral link exactly as given, once, where it belongs for that
channel. Include the paid-partnership disclosure.

Return ONLY a JSON array, no prose around it, of 3 objects:
[{{"channel": "...", "hook": "the first line, on its own", "text": "the full copy"}}]
"""


def generate_ads(db: Session, promoter: Promoter, link: str, *,
                 channel: str, angle: str, provider: str, api_key: str) -> list[dict]:
    """Draft three ads. Raises ProviderError if the model call fails."""
    system = SYSTEM.format(
        name=PRODUCT["name"], what=PRODUCT["what"], who=PRODUCT["who"],
        points="\n".join(f"- {p}" for p in PRODUCT["points"]),
        rules="\n".join(f"- {r}" for r in RULES),
    )
    ask = (f"Channel: {channel}\n"
           f"Referral link to include: {link}\n")
    if angle.strip():
        ask += f"The angle the promoter wants: {angle.strip()}\n"
    ask += "Write three different options. Vary the hook, not just the wording."

    result = providers.run(provider=provider, api_key=api_key, system=system,
                           messages=[{"role": "user", "content": ask}],
                           specs=[], dispatch=lambda *_: "")
    text = (getattr(result, "text", None) or str(result)).strip()

    # Models wrap JSON in prose or a fence often enough that failing on it would
    # make the feature flaky for no reason. Take the array out of the middle.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        drafts = json.loads(text)
    except json.JSONDecodeError:
        # One usable draft beats an error message. The copy is the point; the
        # shape it arrived in is not.
        return [{"channel": channel, "hook": "", "text": (getattr(result, "text", "") or "").strip()}]

    out = []
    for d in drafts if isinstance(drafts, list) else []:
        if not isinstance(d, dict):
            continue
        out.append({"channel": str(d.get("channel") or channel)[:80],
                    "hook": str(d.get("hook") or "")[:300],
                    "text": str(d.get("text") or "")[:4000]})
    return out[:5]


def ads_used_today(db: Session, user_id: int) -> int:
    from .settings_store import _day_start
    try:
        return (db.query(AssistantLog)
                .filter(AssistantLog.user_id == user_id,
                        AssistantLog.region == "ad-copy",
                        AssistantLog.ok.is_(True),
                        AssistantLog.created_at >= _day_start())
                .count())
    except Exception:
        db.rollback()
        return 0
