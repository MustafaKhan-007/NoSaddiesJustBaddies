"""Social-link handling for profiles + Instagram reel embedding.

Creator members can add links to their profile, but only to recognised social
platforms (keeps profiles clean and safe). We also turn a reel/post URL into an
embeddable iframe src for the home-page "Reel of the Week".
"""
import re

#: domain fragment -> nice platform label. First match wins.
PLATFORMS = [
    ("instagram.com", "Instagram"),
    ("tiktok.com", "TikTok"),
    ("youtube.com", "YouTube"),
    ("youtu.be", "YouTube"),
    ("facebook.com", "Facebook"),
    ("fb.com", "Facebook"),
    ("snapchat.com", "Snapchat"),
    ("x.com", "X"),
    ("twitter.com", "X"),
    ("pinterest.com", "Pinterest"),
    ("threads.net", "Threads"),
    ("linkedin.com", "LinkedIn"),
    ("twitch.tv", "Twitch"),
]

ALLOWED_LABELS = sorted({label for _, label in PLATFORMS})


def platform_for(url: str):
    """Return the platform label for a URL, or None if it isn't a known social."""
    host = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0]
    host = host.split("@")[-1]  # ignore any user:pass@
    for frag, label in PLATFORMS:
        if host == frag or host.endswith("." + frag) or host == "www." + frag:
            return label
    return None


def clean_social_links(pairs, limit: int = 6):
    """From [{'label','url'}...] keep only valid social links (label auto-set)."""
    out = []
    for item in pairs:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        label = platform_for(url)
        if not label:
            continue
        out.append({"label": label, "url": url[:300]})
        if len(out) >= limit:
            break
    return out


def instagram_embed_url(url: str):
    """Turn an Instagram reel/post URL into its /embed iframe src, or None."""
    if not url:
        return None
    m = re.search(r"instagram\.com/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    return f"https://www.instagram.com/reel/{m.group(1)}/embed/"
