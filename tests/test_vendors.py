"""Vendor labelling regression tests.

Every case is a real listing observed during verification (docs/findings.md).
The awkward ones are the point: they encode the collisions that a naive
single-field rule gets wrong.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from golfapps.vendors import label_app, load  # noqa: E402

CASES = [
    # (name, row, expected_vendor, expected_confidence)
    ("thorncreek_sagacity", dict(
        bundle_id="com.quick18.thorncreek", seller_name="Quick 18, Inc.",
        artist_name="Quick 18, Inc.", seller_url="http://quick18.com/",
        copyright="© 2018 Quick18, Inc.",
        privacy_policy_url="http://book.quick18.com/content/privacy_policy.htm",
    ), "sagacity", "high"),

    # Migrated Quick18 -> Gallus. seller_url is stale; copyright is current.
    ("los_serranos_vendor_switch", dict(
        bundle_id="com.jcresorts.losserranos", seller_name="JC Resorts, LLC",
        artist_name="JC Resorts, LLC", seller_url="http://quick18.com/",
        copyright="© 2022 Gallus Golf",
        privacy_policy_url="https://manager.gallusgolf.com/privacy",
        support_url="https://support.gallusgolf.com",
    ), "gallus", "high"),

    # Teesnap resells the Gallus codebase: bundle says gallus, seller says Teesnap.
    ("teesnap_on_gallus_bundle", dict(
        bundle_id="com.gallusgolf.c1891.ios.giantoakgc",
        seller_name="Teesnap, LLC", artist_name="Teesnap, LLC",
    ), "teesnap", "high"),

    ("honeybrook_gallus_bundle_only", dict(
        bundle_id="com.gallusgolf.c1176.ios.honeybrookgc",
        seller_name="Honeybrook Golf Club",
    ), "gallus", "high"),

    # Operator-published Sagacity: only sellerUrl names the vendor.
    ("goat_hill_sagacity_seller_url", dict(
        bundle_id="com.goathillpark.goathillpark",
        seller_url="https://www.sagacitygolf.com",
    ), "sagacity", "medium"),

    ("ron_jaworski_is_chronogolf", dict(
        bundle_id="com.chronogolf.booking.jaworskigolf",
        artist_name="Chronogolf, Inc.",
    ), "chronogolf", "high"),

    # Northstar publishes as Sibisoft.
    ("northstar_via_sibisoft", dict(
        bundle_id="com.sibisoft.Jonathan", artist_name="Jonathan Club",
    ), "northstar", "high"),

    # Child vendor sharing the parent's bundle prefix must not collapse into it.
    ("clubhouse_online_under_jonas", dict(
        bundle_id="com.jonassoftware.northshorecountryclu",
        seller_url="https://clubhouseonline-e3.com",
    ), "clubhouseonline", "medium"),

    ("plain_jonas", dict(
        bundle_id="com.jonassoftware.sycamorehillsgolfclu",
        artist_name="Sycamore Hills Golf Club",
    ), "jonas", "high"),

    ("scarsdale_membersfirst", dict(
        bundle_id="com.membersfirst.scarsdalegolfclub",
        artist_name="Scarsdale Golf Club",
    ), "membersfirst", "high"),

    ("farm_neck_clubessential", dict(
        bundle_id="com.clubessential.FarmNeckGolfClub",
        seller_url="https://www.clubessential.com/",
    ), "clubessential", "high"),

    # Copyright naming the vendor must win over another vendor's privacy domain.
    # MembersFirst is owned by Jonas, so its privacy policy points at jonasclub.com --
    # but the product is MembersFirst and the copyright says so.
    ("copyright_token_fallback_beats_parent_privacy", dict(
        bundle_id="com.membersfirst.scarsdalegolfclub",
        seller_name="Scarsdale Golf Club, Inc.",
        copyright="© 2026 MembersFirst",
        privacy_policy_url="https://www.jonasclub.com/privacy-policy/",
    ), "membersfirst", "high"),

    # --- must NOT match ---
    ("credit_union_not_membersfirst", dict(
        bundle_id="com.membersfirstcreditunionproduction",
        seller_name="Members First Credit Union", seller_url="https://mfcu.net",
    ), None, "unknown"),

    ("escooter_not_whoosh", dict(
        bundle_id="whoosh.bike", artist_name="Whoosh Ltd.",
        seller_url="https://whoosh.bike",
    ), None, "unknown"),

    # Genuinely unlabellable from the free JSON -- must escalate to HTML, not guess.
    ("palmbrook_needs_escalation", dict(
        bundle_id="com.palmbrookgolf.palmbrookgolf",
        seller_name="Swing First Golf, LLC",
        seller_url="https://www.palmbrookgolf.com",
    ), None, "unknown"),
]


def test_labels():
    reg = load()
    failures = []
    for name, row, want_vendor, want_conf in CASES:
        got = label_app(row, reg)
        if got.vendor != want_vendor or got.confidence != want_conf:
            failures.append(
                f"{name}: got ({got.vendor}, {got.confidence}) "
                f"want ({want_vendor}, {want_conf})")
    assert not failures, "\n".join(failures)


def test_description_alone_is_never_high_confidence():
    """The Quick18 template appears verbatim on a Gallus-owned listing, so a
    description match identifies a codebase lineage, not a vendor."""
    got = label_app(dict(
        bundle_id="com.example.someclub",
        description=("The app also support promotion code discounts ... and share "
                     "these reservations with your playing partners via text and "
                     "email. Powered by quick18."),
    ))
    assert got.confidence == "low", got


def test_vendor_switch_is_flagged_as_conflict():
    got = label_app(dict(
        bundle_id="com.jcresorts.losserranos", seller_url="http://quick18.com/",
        copyright="© 2022 Gallus Golf",
        privacy_policy_url="https://manager.gallusgolf.com/privacy",
    ))
    assert got.vendor == "gallus"
    assert got.conflict, "a listing naming two vendors must be flagged"
    assert any(r["vendor"] == "sagacity" for r in got.conflict_detail)


if __name__ == "__main__":
    test_labels()
    test_description_alone_is_never_high_confidence()
    test_vendor_switch_is_flagged_as_conflict()
    print(f"all vendor tests passed ({len(CASES)} labelling cases + 2 behaviours)")
