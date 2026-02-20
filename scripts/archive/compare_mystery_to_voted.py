#!/usr/bin/env python3
"""
Compare actual mystery pool addresses (from stakeToken) to user's voted pools.
"""

# User's voted pools
voted_pools = [
    "0x19FF35059452Faa793DdDF9894a1571c5D41003e",
    "0xe62a34Ae5e0B9FdE3501Aeb72DC9585Bb3B72A7e",
    "0xF19787f048b3401546aa7A979afa79D555C114Dd",
    "0xE539b14a87D3Db4a2945ac99b29A69DE61531592",
]

# Mystery pools (from stakeToken() calls)
mystery_pools = [
    ("0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44", "$246.75"),  # from gauge 0xee5f8bf7...
    ("0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c", "$236.11"),  # from gauge 0xe63cd994...
    ("0xEf96Ec76eEB36584FC4922e9fA268e0780170f33", "$245.80"),  # from gauge 0xdc470dc0...
    ("0x680581725840958141Bb328666D8Fc185aC4FA49", "$227.67"),  # from gauge 0x1df220b4...
]

print("\nCOMPARING MYSTERY POOLS TO VOTED POOLS")
print("=" * 80)

print("\nYour voted pools:")
for i, pool in enumerate(voted_pools, 1):
    print(f"  {i}. {pool}")

print("\nMystery pools (where rewards came from):")
for i, (pool, reward) in enumerate(mystery_pools, 1):
    print(f"  {i}. {pool} (paid {reward})")

print("\n" + "=" * 80)
print("MATCHING:")
print("=" * 80)

matches_found = 0
for pool, reward in mystery_pools:
    if pool.lower() in [p.lower() for p in voted_pools]:
        print(f"✓ MATCH: {pool} (paid {reward})")
        print(f"  This IS one of your voted pools!")
        matches_found += 1
    else:
        print(f"❌ NO MATCH: {pool} (paid {reward})")
        print(f"  This is NOT one of your voted pools")

print("\n" + "=" * 80)
print(f"SUMMARY: {matches_found}/4 mystery pools matched your voted pools")
print("=" * 80)

if matches_found == 0:
    print("\n⚠️  Still zero matches! Even with correct pool addresses,")
    print("   you received ALL rewards from pools you didn't vote for.")
    print("\n   This means:")
    print("   1. Rewards are distributed proportionally across all voters")
    print("   2. OR there's epoch/timing mismatch in the claim")
    print("   3. OR these are protocol-wide fee distribution pools")
