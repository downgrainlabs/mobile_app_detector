"""Apply Derek's explicit corrections from the reviewed residue CSV, and remove the
confirmed non-US links. Every removal/correction here has an explicit NOTE from
Derek's review, not an algorithmic guess.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

# ---- explicit corrections (Derek found the exact domain match himself) ----------
CORRECTIONS = [
    (1177073316, "The Lakes Country Club", 12658, "thelakescc.com -- exact domain match in facility table"),
    (1305628325, "Raintree Country Club.", 14381, "raintreecountryclub.com -- exact domain match in facility table"),
    (1466636965, "Inverness Golf Club App", 5314, "invernessgolfclub.org -- exact domain match in facility table"),
]

# ---- confirmed non-US: remove the wrong US link, no replacement (yet) -----------
REMOVALS = [
    (1118436143, "York Golf Club", "wholeinonegolf.co.uk -- UK (Yorkshire), not US"),
    (1348848507, "Exeter Golf and Country Club", "exetergcc.co.uk -- UK"),
    (1457309093, "Hampton Golf Club", "hamptongolf.ca -- Canadian; app was double-linked to 2 wrong US facilities"),
    (1506995197, "Chester Golf Club", "chestergolfclub.ca -- Canadian"),
    (1542012561, "Beaverbrook Golf Club", "coursemateapp.co.uk -- UK app-builder platform"),
    (1619087798, "Meadow Springs Golf Club", "msgcc.com.au -- Australian"),
    (1622403131, "The Lakes Golf Club & Resort", "lakesresort.ca -- Canadian"),
    (6738953628, "Grand Golf Club", "thegrandgolfclub.com.au -- Australian"),
    (6754680278, "The Hills Golf Club", "thehills.co.nz -- New Zealand"),
    (6760475467, "St. Charles CC", "stcharlescountryclub.ca -- Canadian"),
]

# ---- landings club: keep the confirmed one, remove the duplicate ----------------
DEDUP_KEEP = [(1259202474, "The Landings Club", 13184, 4051)]

print("--- corrections ---")
for tid, name, fid, note in CORRECTIONS:
    old = db.query(f"SELECT facility_id FROM ga_facility_app WHERE track_id={tid} "
                   f"AND match_method='match_service'")
    for o in old:
        db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid} "
                   f"AND facility_id={o['facility_id']}")
    db.upsert("ga_facility_app", [{
        "facility_id": fid, "track_id": tid, "match_confidence": 1.0,
        "match_method": "domain",
        "match_status": f"Corrected by Derek review: {note}",
    }], on_conflict="facility_id,track_id")
    print(f"  {name}: -> #{fid} ({note})")

print("\n--- non-US removals ---")
for tid, name, note in REMOVALS:
    n = db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid} "
                   f"AND match_method='match_service'")
    print(f"  {name}: removed match_service link(s) ({note})")

print("\n--- dedup: keep confirmed, drop the other ---")
for tid, name, keep_fid, drop_fid in DEDUP_KEEP:
    db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid} AND facility_id={drop_fid}")
    db.upsert("ga_facility_app", [{
        "facility_id": keep_fid, "track_id": tid, "match_confidence": 1.0,
        "match_method": "domain", "match_status": "Confirmed by Derek review",
    }], on_conflict="facility_id,track_id")
    print(f"  {name}: kept #{keep_fid}, removed #{drop_fid}")

print("\ndone")
