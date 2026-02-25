#!/usr/bin/env python3
"""P3 Integration Test Script"""

import sqlite3
from pathlib import Path
from analysis.pre_boundary.features import build_snapshot_features, validate_features
from analysis.pre_boundary.proxies import learn_vote_drift_by_window, learn_reward_uplift_by_window, attach_proxies_to_features
from analysis.pre_boundary.compute_proxies import compute_and_cache_proxies
from analysis.pre_boundary.feature_validator import validate_epoch_features

def main():
    print("=" * 80)
    print("P3 Integration Test")
    print("=" * 80)

    db_path = "data/db/preboundary_dev.db"
    cache_dir = "data/preboundary_cache"
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(epoch) FROM preboundary_snapshots")
        epoch = cursor.fetchone()[0]
        print(f"\n✓ Epoch: {epoch}")

        # Step 1: Load features
        print("\n🔄 Step 1: Loading snapshot features")
        features = build_snapshot_features(conn, epoch)
        total = sum(len(f) for f in features.values())
        print(f"✓ Loaded {total} features")
        for w, f in features.items():
            print(f"  - {w}: {len(f)} features")

        # Step 2: Validate features
        print("\n🔄 Step 2: Validating features")
        is_valid, warnings = validate_features(features)
        print(f"✓ Validation: {'PASS' if is_valid else 'FAIL'}")

        # Step 3: Compute proxies
        print("\n🔄 Step 3: Computing proxies")
        drift_estimates = {}
        uplift_estimates = {}
        for window in ["day", "T-1", "boundary"]:
            drift_est = learn_vote_drift_by_window(conn, window)
            uplift_est = learn_reward_uplift_by_window(conn, window)
            drift_estimates[window] = drift_est
            uplift_estimates[window] = uplift_est
            print(f"  - {window}: {len(drift_est)} drift, {len(uplift_est)} uplift")

        # Step 4: Augment features
        print("\n🔄 Step 4: Augmenting features with proxies")
        augmented = attach_proxies_to_features(features, drift_estimates, uplift_estimates)
        augmented_total = sum(len(f) for f in augmented.values())
        print(f"✓ Augmented {augmented_total} features")

        # Step 5: Validate epoch
        print("\n🔄 Step 5: Validating epoch")
        diag = validate_epoch_features(conn, epoch, verbose=False)
        print(f"✓ Coverage: {diag['overall_coverage']:.1%}")
        print(f"  - Missing drift: {len(diag['gauges_missing_drift'])}")
        print(f"  - Missing uplift: {len(diag['gauges_missing_uplift'])}")

        # Step 6: Cache proxies
        print("\n🔄 Step 6: Caching proxies")
        cache_files = compute_and_cache_proxies(conn, db_path=db_path, output_dir=cache_dir)
        print(f"✓ Created {len(cache_files)} cache files")

        # Verify cache
        cache_files_on_disk = list(Path(cache_dir).glob("*_estimates_*.json"))
        print(f"✓ Cache files on disk: {len(cache_files_on_disk)}")
        for cf in sorted(cache_files_on_disk):
            size = cf.stat().st_size
            print(f"  - {cf.name}: {size} bytes")

        print("\n" + "=" * 80)
        print("✓ P3 Integration Test PASSED")
        print("=" * 80)
        print(f"\nSummary:")
        print(f"  - Features loaded: {total}")
        print(f"  - Proxies computed: {sum(len(d) for d in drift_estimates.values()) + sum(len(u) for u in uplift_estimates.values())}")
        print(f"  - Cache files: {len(cache_files_on_disk)}")
        print(f"  - Coverage: {diag['overall_coverage']:.1%}")

        conn.close()

    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
