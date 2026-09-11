"""Apply the state-hint rescue pass to the current Phase 2 residue.

Many course-app track_names carry their own state as a trailing suffix
("EagleRock Golf Course - MT"), which join.py's candidate_name() previously
sent to match-service as noise in the name instead of splitting out as the
state filter it actually is (fixed in join.py -- see strip_trailing_state()).

For the 21 residue rows where this hint is present, resolved locally against
facility.state_code (no match-service call needed):
  CONFIRM    -- single live option, its state matches the hint  (14 rows)
  DUP_RESOLVE -- two live options, exactly one's state matches the hint (3 rows)
  CORRECT    -- single live option, state does NOT match the hint, and a
                distinct correctly-named+stated facility exists  (4 rows)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

CONFIRM = [1451560919, 1497409568, 1510497567, 1540172373, 1567460693,
          1603938290, 1623466499, 6475613167, 6478895125, 6754931775,
          6760196749, 6760318034, 6760318038, 6764659472]

DUP_RESOLVE = [
    (1372311829, "The Orchards Golf Club", 6784, 6071),   # keep MA, drop KS
    (1501448292, "Shoreline Golf Links", 3432, 5766),      # keep CA, drop IA
    (6450279778, "Brentwood Golf Course", 4185, 7358),     # keep FL, drop MI
]

CORRECT = [
    (1458568763, "Pleasant View Golf Course", 15310, 7277),  # -> Pleasant View GC, Middleton WI
    (1505415217, "White Deer Golf Course", 14728, 7253),     # -> White Deer GC, Montgomery PA
    (6446255022, "Beaver Meadows Golf Club", 8877, 5919),    # -> Beaver Meadows G&R, Phoenix NY
    (6740289246, "Redhawk Golf Course", 3698, 4945),         # -> Redhawk Golf Club, Temecula CA
]

print("--- confirm (state hint matches the live link) ---")
for tid in CONFIRM:
    db.exec_sql(f"UPDATE ga_facility_app SET match_status = "
               f"'Confirmed by app-name state hint' WHERE track_id={tid}")
    print(f"  {tid}: confirmed")

print("\n--- duplicate-link resolved by state hint ---")
for tid, name, keep_fid, drop_fid in DUP_RESOLVE:
    db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid} AND facility_id={drop_fid}")
    db.upsert("ga_facility_app", [{
        "facility_id": keep_fid, "track_id": tid, "match_confidence": 1.0,
        "match_method": "state_hint",
        "match_status": "Resolved by app-name state hint (2 live options, 1 matched)",
    }], on_conflict="facility_id,track_id")
    print(f"  {name}: kept #{keep_fid}, dropped #{drop_fid}")

print("\n--- corrected (state hint contradicted the live link) ---")
for tid, name, correct_fid, wrong_fid in CORRECT:
    db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid} AND facility_id={wrong_fid}")
    db.upsert("ga_facility_app", [{
        "facility_id": correct_fid, "track_id": tid, "match_confidence": 1.0,
        "match_method": "state_hint",
        "match_status": "Corrected by app-name state hint -- live link's state didn't match",
    }], on_conflict="facility_id,track_id")
    print(f"  {name}: #{wrong_fid} -> #{correct_fid}")

print("\ndone -- 14 confirmed, 3 duplicate-link resolutions, 4 corrections")
