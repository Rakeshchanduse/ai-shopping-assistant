"""
configurable_doctor.py - see exactly why a configurable's variants / labels / stock
are missing. Run on the SAME machine as the app (so magento.test resolves):

    python configurable_doctor.py            # defaults to MH01 (Luma)
    python configurable_doctor.py WS12       # any configurable sku

It prints the raw pieces the variant logic depends on, so we can pinpoint the break:
the parent's configurable_product_options, a child's custom_attributes + stock_item,
the attribute-id -> code and value -> label lookups, and the final _variants() output.
"""

import os
import sys
import json

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from magento_client import MagentoClient

SKU = sys.argv[1] if len(sys.argv) > 1 else "MH01"


def show(label, obj, limit=1500):
    txt = json.dumps(obj, indent=2, default=str)
    print(f"{label}: {txt[:limit]}{' ...(truncated)' if len(txt) > limit else ''}")


def main():
    c = MagentoClient(base_url=os.getenv("MAGENTO_BASE_URL"))
    print(f"REST base: {c.rest}\nSKU: {SKU}\n" + "=" * 60)

    # 1) parent product
    try:
        p = c._get(f"{c.rest}/products/{SKU}", token=c.ensure_admin())
    except Exception as e:  # noqa: BLE001
        print(f"FAIL fetching product {SKU}: {type(e).__name__}: {e}")
        return
    print(f"type_id        = {p.get('type_id')}")
    print(f"price          = {p.get('price')}")
    print(f"parent stock   = {(p.get('extension_attributes') or {}).get('stock_item')}")

    if p.get("type_id") != "configurable":
        print("\n!! This SKU is not a configurable product. Pass a configurable sku, e.g. MH01 / WS12.")
        return

    # 2) configurable options on the parent
    opts = (p.get("extension_attributes") or {}).get("configurable_product_options")
    print("\n--- configurable_product_options on parent ---")
    if not opts:
        print("!! EMPTY - the parent has no configurable_product_options. Labels cannot be built.")
    else:
        for o in opts:
            print(f"  option: attribute_id={o.get('attribute_id')} label={o.get('label')!r} "
                  f"values={len(o.get('values') or [])}")

    # 3) children
    print("\n--- children ---")
    try:
        children = c._get(f"{c.rest}/configurable-products/{SKU}/children", token=c.ensure_admin())
    except Exception as e:  # noqa: BLE001
        print(f"!! FAIL fetching children: {type(e).__name__}: {e}")
        children = []
    print(f"children count = {len(children or [])}")
    if children:
        ch = children[0]
        print(f"  first child sku   = {ch.get('sku')}")
        print(f"  first child price = {ch.get('price')}")
        print(f"  child stock_item  = {(ch.get('extension_attributes') or {}).get('stock_item')} "
              f"(None => /children omits it; _is_salable fallback is used)")
        ca = {a.get('attribute_code'): a.get('value') for a in (ch.get('custom_attributes') or [])}
        interesting = {k: v for k, v in ca.items() if k in ("color", "size") or "size" in k or "color" in k}
        print(f"  child color/size custom_attributes = {interesting or '(none found - check codes below)'}")

    # 4) attribute id -> code, and value -> label
    print("\n--- attribute resolution ---")
    for o in (opts or []):
        aid = o.get("attribute_id")
        code = c._attr_code(aid)
        print(f"  attribute_id {aid} -> code {code!r}")
        if code:
            sample = dict(list(c._attr_options(code).items())[:5])
            print(f"    options sample (value->label): {sample}")
            if children:
                val = c._attr(children[0], code)
                print(f"    first child's {code} value = {val!r} -> "
                      f"{c._attr_options(code).get(str(val), '(no label)')}")

    # 5) the actual outputs used by the app
    print("\n--- _variants(parent) ---")
    show("variants", c._variants(p))
    print("\n--- _configurable_summary(sku) ---")
    print(c._configurable_summary(SKU))
    print("\n--- get_product(sku) (what the detail page shows) ---")
    show("detail", c.get_product(SKU), limit=2000)


if __name__ == "__main__":
    main()
