"""Course recommendations from a member's stated intent ("what brings you here").

Each intent maps to a set of tag keywords. Products carry hidden tags (set in
the admin) and are scored by how many of their tags match the member's intents.
"""
from ..models import Product

#: gentle-yet-determined onboarding options. `tags` are matched against the
#: hidden tags an admin puts on each product.
INTENTS = [
    {"key": "content_creator",
     "label": "I want to grow as a content creator",
     "tags": ["content", "creator", "audience", "instagram", "brand", "social"]},
    {"key": "divorce",
     "label": "I'm finding my feet after a divorce or breakup",
     "tags": ["divorce", "breakup", "heartbreak", "starting-over", "single"]},
    {"key": "custody",
     "label": "I'm navigating co-parenting or custody",
     "tags": ["custody", "co-parenting", "parenting", "kids", "family"]},
    {"key": "confidence",
     "label": "I'm rebuilding my confidence",
     "tags": ["confidence", "self-worth", "mindset", "boundaries"]},
    {"key": "grief",
     "label": "I'm carrying grief or loss",
     "tags": ["grief", "loss", "healing"]},
    {"key": "career",
     "label": "I'm starting over in work or money",
     "tags": ["career", "money", "work", "business", "purpose"]},
    {"key": "routine",
     "label": "I want gentler, steadier daily habits",
     "tags": ["habits", "routine", "morning", "discipline", "focus"]},
    {"key": "exploring",
     "label": "I'm just here to look around, softly",
     "tags": []},
]

_INTENT_TAGS = {i["key"]: set(i["tags"]) for i in INTENTS}
INTENT_LABELS = {i["key"]: i["label"] for i in INTENTS}


def valid_intent_keys(keys) -> list:
    """Keep only recognised intent keys, in a stable order."""
    incoming = set(keys or [])
    return [i["key"] for i in INTENTS if i["key"] in incoming]


def _keywords_for(goal_keys) -> set:
    words = set()
    for key in goal_keys or []:
        words |= _INTENT_TAGS.get(key, set())
    return words


def _score(product_tags, keywords) -> int:
    score = 0
    for tag in product_tags:
        tag = tag.lower()
        for kw in keywords:
            if kw == tag or kw in tag or tag in kw:
                score += 1
                break
    return score


def recommend_products(user, limit: int = 3) -> list:
    """Published products that best match the member's intents.

    Falls back to featured, then newest, when there's no signal — so this never
    returns an empty shelf while products exist.
    """
    published = (Product.query.filter_by(status="published")
                 .order_by(Product.sort_order, Product.created_at.desc()).all())
    if not published:
        return []

    keywords = _keywords_for(user.goals() if user and user.is_authenticated else [])
    if keywords:
        scored = [(p, _score(p.tags(), keywords)) for p in published]
        matches = sorted([ps for ps in scored if ps[1] > 0],
                         key=lambda ps: ps[1], reverse=True)
        if matches:
            return [p for p, _ in matches[:limit]]

    featured = [p for p in published if p.featured]
    return (featured or published)[:limit]
