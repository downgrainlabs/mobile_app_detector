import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SERPAPI_KEY', '8801169da5be447a04dfab1fe8101ccbc6555e7b49a124b337d28a8bb9066455')
from golfapps import lens

ids = [6771023855, 943470455, 1124950697, 1571143862, 986011045, 1221000877, 1315769309,
       1078270716, 1094179490, 1155411292, 1102570263, 1108582206, 1231581169, 1117514715,
       1119634801, 1124170134, 1156910282, 1171801525, 1196443594, 1268751136, 1357158919,
       1359959915, 1389045730, 6476159748, 6759293201, 1536936241, 6474169885, 6618141489,
       1545326024, 1556466812, 1556469070, 1604513036, 1644324260, 6478382065, 6737781976,
       6457545376, 6469359283, 6503928413, 6477495285, 6502668573, 6503406228, 6498890547,
       6502586693, 6756355649, 6499241277, 6746460967, 6754613691, 6755246581, 6753666381,
       6754444042]

print(f"starting: {len(ids)} track_ids", flush=True)
try:
    stats = lens.run_with_matcher(ids, apply_links=True)
    print("===RESULT===", flush=True)
    print(json.dumps(stats.as_dict(), indent=2, default=str), flush=True)
except Exception:
    import traceback
    traceback.print_exc()
    raise
print("DONE", flush=True)
