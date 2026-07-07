"""
search_doctor.py - see how the product finder resolves shopper phrases.

    python search_doctor.py
    python search_doctor.py sweatshirt "women sweatshirt" backpack bags "women tops"

For each query it prints the facet ATTRIBUTE match (e.g. style_general=Sweatshirt), the attribute
SET match (e.g. Bag), and which STRATEGY find_products used (filter / category / attribute /
attribute_set / search) with the top products. Also lists the discovered facet attributes and
attribute sets so you can confirm Style General / Bag / Top etc. are picked up.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from magento_client import MagentoClient

QUERIES = sys.argv[1:] or ["sweatshirt", "women sweatshirt", "hoodie", "backpack",
                           "bags", "women tops", "cotton", "yoga"]


def main():
    c = MagentoClient(base_url=os.getenv("MAGENTO_BASE_URL"))
    print(f"REST base: {c.rest}\n" + "=" * 66)

    attrs = c._filterable_attributes()
    print(f"facet attributes discovered: {len(attrs)}")
    for a in attrs:
        sample = [lbl for _, lbl in a["options"][:6]]
        print(f"  {a['code']:22} {'multi ' if a['multi'] else 'select'} "
              f"{len(a['options']):3} opts  e.g. {sample}")

    sets = c._attribute_sets()
    print(f"\nattribute sets: {[s.get('attribute_set_name') for s in sets]}")
    print("=" * 66)

    for q in QUERIES:
        attr = c.match_attribute_value(q)
        aset = c.match_attribute_set(q)
        res = c.find_products(q, limit=5)
        print(f"\nquery: {q!r}")
        print(f"  attribute match     : {attr}")
        print(f"  attribute-set match : {aset}")
        print(f"  find_products        : method={res['method']} matched={res['matched']!r} "
              f"count={len(res['products'])}")
        for p in res["products"][:5]:
            print(f"     - {p.get('sku'):16} {p.get('name')}")


if __name__ == "__main__":
    main()
