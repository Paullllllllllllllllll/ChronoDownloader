#!/usr/bin/env bash
# run_matrix.sh -- Execute the ChronoDownloader staging test matrix
#
# Usage:
#   bash tests/staging/run_matrix.sh              # full matrix
#   bash tests/staging/run_matrix.sh --tier 1     # smoke tests only
#   bash tests/staging/run_matrix.sh --search     # search mode only
#   bash tests/staging/run_matrix.sh --iiif       # direct IIIF only

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

CONFIG="tests/staging/config_staging.json"
OUTDIR="tests/staging/output"
SEARCH_CSV="tests/staging/staging_search.csv"
IIIF_CSV="tests/staging/staging_direct_iiif.csv"

TIER_FILTER=""
MODE_FILTER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tier)  TIER_FILTER="$2"; shift 2 ;;
        --search) MODE_FILTER="search"; shift ;;
        --iiif)  MODE_FILTER="iiif"; shift ;;
        *)       echo "Unknown option: $1"; exit 1 ;;
    esac
done

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0
declare -a RESULTS=()

run_cell() {
    local mode="$1"      # "search" or "iiif"
    local entry_id="$2"
    local provider="$3"  # provider key (search) or "auto" (iiif)
    local tier="$4"
    local label="$5"

    if [[ -n "$TIER_FILTER" && "$tier" != "$TIER_FILTER" ]]; then
        RESULTS+=("SKIP  $label  (tier $tier filtered out)")
        SKIP_COUNT=$((SKIP_COUNT + 1))
        return 0
    fi

    if [[ -n "$MODE_FILTER" && "$mode" != "$MODE_FILTER" ]]; then
        RESULTS+=("SKIP  $label  (mode filtered out)")
        SKIP_COUNT=$((SKIP_COUNT + 1))
        return 0
    fi

    local csv
    local extra_flags=""
    if [[ "$mode" == "search" ]]; then
        csv="$SEARCH_CSV"
        extra_flags="--providers $provider"
    else
        csv="$IIIF_CSV"
    fi

    local logfile="$OUTDIR/${entry_id}.log"
    echo ""
    echo "================================================================"
    echo "  [$label]  entry=$entry_id  provider=$provider  tier=$tier"
    echo "================================================================"

    set +e
    python -m main.cli "$csv" \
        --config "$CONFIG" --cli \
        --entry-ids "$entry_id" \
        --output_dir "$OUTDIR" \
        --log-level DEBUG \
        $extra_flags \
        2>&1 | tee "$logfile"
    local rc=${PIPESTATUS[0]}
    set -e

    if [[ $rc -eq 0 ]]; then
        local obj_count
        obj_count=$(find "$OUTDIR" -path "*${entry_id}*/objects/*" -type f 2>/dev/null | wc -l)
        if [[ $obj_count -gt 0 ]]; then
            RESULTS+=("PASS  $label  ($obj_count file(s) downloaded)")
            PASS_COUNT=$((PASS_COUNT + 1))
        else
            RESULTS+=("WARN  $label  (exit 0 but no files in objects/)")
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    else
        RESULTS+=("FAIL  $label  (exit code $rc)")
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
}

echo "ChronoDownloader Staging Test Matrix"
echo "====================================="
echo "Start time: $(date -Iseconds)"
echo ""

rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

# ── Tier 1: Smoke tests (fastest, most reliable) ──────────────

run_cell iiif   D_MDZ   auto              1 "direct-iiif/mdz"
run_cell search S_MDZ   mdz               1 "search/mdz"
run_cell iiif   D_IA    auto              1 "direct-iiif/ia"
run_cell search S_IA    internet_archive  1 "search/ia"
run_cell search S_ERARA e_rara            1 "search/e-rara"

# ── Tier 2: No auth, moderate speed ───────────────────────────

run_cell search S_LOC   loc               2 "search/loc"
run_cell search S_SLUB  slub              2 "search/slub"
run_cell search S_SBB   sbb_digital       2 "search/sbb"
run_cell search S_POLONA polona           2 "search/polona"
run_cell search S_WELLCOME wellcome       2 "search/wellcome"
run_cell iiif   D_WELLCOME auto           2 "direct-iiif/wellcome"
run_cell search S_BNE   bne              2 "search/bne"
run_cell search S_BL    british_library  2 "search/bl"

# ── Tier 3: Rate-limited providers ────────────────────────────

run_cell iiif   D_GAL   auto              3 "direct-iiif/gallica"
run_cell search S_GAL   bnf_gallica       3 "search/gallica"
run_cell search S_AA    annas_archive     3 "search/annas-archive"

# ── Tier 4: Non-IIIF providers ────────────────────────────────

run_cell search S_GB    google_books      4 "search/google-books"
run_cell search S_HT    hathitrust        4 "search/hathitrust"

# ── Tier 5: API-key-gated (conditional) ───────────────────────

run_cell search S_EUR   europeana         5 "search/europeana"
run_cell search S_DDB   ddb               5 "search/ddb"

# ── Summary ───────────────────────────────────────────────────

echo ""
echo ""
echo "================================================================"
echo "  TEST MATRIX SUMMARY"
echo "================================================================"
echo ""

for result in "${RESULTS[@]}"; do
    echo "  $result"
done

echo ""
echo "  Total: $((PASS_COUNT + FAIL_COUNT + SKIP_COUNT))  |  Pass: $PASS_COUNT  |  Fail: $FAIL_COUNT  |  Skip: $SKIP_COUNT"
echo "  End time: $(date -Iseconds)"
echo ""

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "  Some tests FAILED. Check logs in $OUTDIR/*.log"
    exit 1
fi
echo "  All executed tests PASSED."
