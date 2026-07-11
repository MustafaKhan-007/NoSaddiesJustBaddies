"""Render every badge (all categories x tiers + owner) to instance/badge_preview.html
so the art can be eyeballed in a browser. Run: python scripts/badge_preview.py
"""
import os
import sys
import xml.dom.minidom as minidom
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LEMONSQUEEZY_WEBHOOK_SECRET", "x")

from flask import render_template_string

from app import create_app
from app.config import DevConfig
from app.extensions import db
from app.services.badges import CATEGORIES, OWNER_BADGE, badge_dict


class PreviewConfig(DevConfig):
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"


app = create_app(PreviewConfig)

DEFS = "{% include 'partials/badge_defs.html' %}"
ONE = "{% from 'partials/badges.html' import badge_svg %}{{ badge_svg(b, 'lg') }}"

with app.test_request_context():
    db.create_all()
    defs = render_template_string(DEFS)
    # validate the shared defs are well-formed XML
    minidom.parseString(defs)

    rows = []
    for key, cat in CATEGORIES.items():
        cells = []
        for level in range(1, len(cat["tiers"]) + 1):
            b = badge_dict(key, level)
            svg = render_template_string(ONE, b=b)
            minidom.parseString(svg.strip())  # raises if malformed
            cells.append(f'<figure>{svg}<figcaption><b>{b["title"]}</b><br>'
                         f'<small>{b["phrase"]}</small></figcaption></figure>')
        rows.append(f'<h2>{cat["name"]}</h2><div class="row">{"".join(cells)}</div>')

    owner_svg = render_template_string(ONE, b=OWNER_BADGE)
    minidom.parseString(owner_svg.strip())
    rows.append('<h2>Owner</h2><div class="row"><figure>' + owner_svg +
                f'<figcaption><b>{OWNER_BADGE["title"]}</b></figcaption></figure></div>')

html = f"""<!doctype html><meta charset=utf-8><title>Badge preview</title>
<style>
body{{background:#FAF5EE;font-family:'Nunito Sans',sans-serif;color:#2B2622;padding:40px;}}
h2{{font-family:Fraunces,Georgia,serif;color:#7A2E62;margin:36px 0 8px;}}
.row{{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-end;}}
figure{{margin:0;text-align:center;width:110px;}}
figure svg{{width:96px;height:auto;filter:drop-shadow(0 3px 6px rgba(43,38,34,.2));}}
figcaption{{margin-top:6px;font-size:13px;}}
small{{color:#6B6159;}}
</style>
{defs}
<h1>First Light — badge collection</h1>
{''.join(rows)}
"""

out = Path(__file__).resolve().parents[1] / "instance" / "badge_preview.html"
out.parent.mkdir(exist_ok=True)
out.write_text(html, encoding="utf-8")
print("All badge SVGs are valid XML. Preview written to:", out)
