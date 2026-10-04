"""Parse every cached page into a canonical PageDocument.

Usage:
    python scripts/03_parse.py [--limit N]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ams.config import force_utf8, load_config  # noqa: E402
from ams.pipeline import parse_all  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Parse only the first N pages")
    args = parser.parse_args()

    force_utf8()
    config = load_config()
    documents, report = parse_all(config, limit=args.limit)

    print(f"parsed {len(report.parsed)}/{report.total} page(s)")
    for page_type, count in sorted(report.by_type.items()):
        print(f"  {page_type:16} {count}")

    if report.failed:
        print(f"\n{len(report.failed)} failure(s):")
        for url, error in report.failed.items():
            print(f"  {url}\n    {error}")

    products = [d for d in documents if d.page_type == "product"]
    with_price = sum(1 for d in products if d.offer and d.offer.price)
    with_specs = sum(1 for d in products if d.specs)
    with_faqs = sum(1 for d in products if d.faqs)
    print(
        f"\nproducts: {len(products)}  "
        f"with price: {with_price}  with specs: {with_specs}  with FAQs: {with_faqs}"
    )

    from ams.pipeline import load_shared

    shared = load_shared(config)
    print(f"\nsite shared: nav={len(shared.navigation)} gst={shared.gst_numbers} "
          f"phones={shared.phone_numbers}")
    print(f"  addresses: {list(shared.addresses)}")
    print(f"  inquiry form fields: {shared.inquiry_form_fields}")

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
