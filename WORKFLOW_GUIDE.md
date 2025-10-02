# Large-Scale Workflow Guide

This guide provides best practices for running large-scale downloads (50+ items) with the ChronoDownloader, based on extensive testing and optimization work.

**Purpose**: This is an operational guide focused on workflows, batch processing, monitoring, and quality control for large-scale operations. For technical details about individual configuration options, see CONFIG_GUIDE.md.

## Table of Contents

1. [Overview](#overview)
2. [Planning Your Workflow](#planning-your-workflow)
3. [Configuration Optimization](#configuration-optimization)
4. [Batch Processing Strategy](#batch-processing-strategy)
5. [Performance Tuning](#performance-tuning)
6. [Monitoring and Recovery](#monitoring-and-recovery)
7. [Quality Control](#quality-control)
8. [Storage Management](#storage-management)

---

## Overview

### Key Principles for Large-Scale Downloads

1. **Test Small, Scale Gradually**: Always validate with 5-10 items before running large batches
2. **Optimize Configuration**: Proper config tuning can improve speed by 60%+
3. **Prepare Quality Input**: Good CSV preparation directly impacts success rates
4. **Monitor Progress**: Real-time monitoring helps catch issues early
5. **Plan for Failures**: 60-70% success rate is typical; have a retry strategy

### Performance Expectations

| Scale | Time Estimate | Success Rate | Recommended Approach |
|-------|--------------|--------------|---------------------|
| 1-10 items | 2-10 minutes | 70-80% | Quick validation |
| 11-25 items | 10-30 minutes | 65-75% | Single batch |
| 26-50 items | 30-90 minutes | 60-70% | Consider splitting |
| 51-100 items | 1-3 hours | 60-70% | Multiple batches recommended |
| 100+ items | 3+ hours | 60-70% | Definitely use batches, run overnight |

---

## Planning Your Workflow

### Step 1: Define Your Collection

**Key Questions:**
- How many items? (Scale determines strategy)
- What types of materials? (Books, manuscripts, images, etc.)
- What languages/regions? (Affects provider selection)
- What formats needed? (PDFs, images, metadata only)
- What quality level? (Complete works vs. samples)

### Step 2: Select Providers

**Provider Selection Matrix:**

| Content Type | Primary Providers | Secondary Providers |
|-------------|------------------|---------------------|
| German materials | MDZ, DDB | Internet Archive, Google Books |
| French materials | BnF Gallica | Internet Archive, Google Books |
| Italian materials | MDZ, Internet Archive | Google Books |
| English materials | Internet Archive, Google Books | Library of Congress, HathiTrust |
| Medical/Scientific | Wellcome | Internet Archive, MDZ |
| General/Mixed | Internet Archive, Google Books | MDZ, Gallica, LoC |

**Provider Characteristics:**

**MDZ (Bavarian State Library)**
- ✅ Excellent IIIF implementation (fast, reliable)
- ✅ Strong coverage: German, Italian, Latin works
- ✅ Complete digitizations (500+ pages common)
- ⚠️ Limited to European materials

**BnF Gallica**
- ✅ Comprehensive French collections
- ✅ Good metadata quality
- ⚠️ Slower API (use 1500ms delay)
- ✅ IIIF manifests usually available

**Internet Archive**
- ✅ Broadest coverage globally
- ✅ PDF availability usually good
- ⚠️ IIIF manifests often fail (optimization critical!)
- ✅ Community uploads = unexpected finds

**Google Books**
- ✅ Fast API, good PDF/EPUB support
- ✅ Strong English-language coverage
- ⚠️ Many items are preview-only
- ✅ Good for 19th-20th century works

**Library of Congress**
- ✅ Authoritative US materials
- ✅ Good metadata quality
- ⚠️ Smaller digitized collection
- ✅ Historical American documents

### Step 3: Create CSV Files

**Best Practices:**

1. **Research Titles First**
   - Search manually on target providers
   - Note exact title formats used
   - Keep original language/spelling
   - Document variants in separate rows

2. **Structure for Success**
   ```csv
   Title,Creator,Date
   "Exact Title from Library Catalog","Creator as Listed",1599
   ```

3. **Quality Checks**
   - ✅ No extra quotes or special characters
   - ✅ Consistent date format (YYYY)
   - ✅ Creator names in original form
   - ✅ Diacritics preserved

4. **Batch Organization**
   ```
   batch_01_german_cookbooks.csv    (25 items)
   batch_02_french_cookbooks.csv    (25 items)
   batch_03_italian_cookbooks.csv   (25 items)
   batch_04_misc_cookbooks.csv      (25 items)
   ```

---

## Configuration Optimization

### Critical Optimization: Internet Archive IIIF

**Problem:** Internet Archive IIIF manifests frequently return 500 errors, causing 5+ minutes of retries per item.

**Solution:** Enable PDF preference to skip IIIF entirely.

```json
{
  "provider_settings": {
    "internet_archive": {
      "prefer_pdf": true,
      "max_pages": 0
    }
  },
  "download": {
    "prefer_pdf_over_images": true
  }
}
```

**Impact:** 60% speed improvement for Internet Archive downloads!

### Network Settings Tuning

**For Large-Scale Runs:**

```json
{
  "provider_settings": {
    "internet_archive": {
      "network": {
        "delay_ms": 300,
        "max_attempts": 5,        // Reduced from 10
        "base_backoff_s": 1.5,
        "backoff_multiplier": 1.5,
        "timeout_s": 30
      }
    },
    "gallica": {
      "network": {
        "delay_ms": 1500,         // Higher for Gallica (slower API)
        "max_attempts": 5,
        "timeout_s": 30
      }
    }
  }
}
```

**Rationale:**
- Fewer retries (`max_attempts: 5`) = faster failure detection
- Appropriate delays prevent rate limiting
- Reasonable timeouts avoid hanging

### Fuzzy Matching Configuration

**For Multilingual Collections:**
```json
{
  "selection": {
    "min_title_score": 50,      // Permissive for Latin/French/Italian/German
    "creator_weight": 0.2,
    "year_tolerance": 5
  }
}
```

**For English-Only Collections:**
```json
{
  "selection": {
    "min_title_score": 65,      // More strict for consistent language
    "creator_weight": 0.3,
    "year_tolerance": 5
  }
}
```

**For Known-Exact Collections:**
```json
{
  "selection": {
    "min_title_score": 75,      // Very strict
    "creator_weight": 0.4,
    "year_tolerance": 3
  }
}
```

### Download Limits

**Prevent Runaway Downloads:**

```json
{
  "download_limits": {
    "total": {
      "images_gb": 100,    // Total across all items
      "pdfs_gb": 50,
      "metadata_gb": 1
    },
    "per_work": {
      "images_gb": 5,      // Stops at 5GB per item
      "pdfs_gb": 3,
      "metadata_mb": 10
    },
    "on_exceed": "stop"    // Options: "stop", "skip", "warn"
  }
}
```

**Storage Planning:**
- Metadata only: ~1-5 MB per item
- PDF downloads: ~10-200 MB per item
- Full image sets: ~100-500 MB per item (500+ pages)
- Budget 500MB-1GB per item for complete works

---

## Batch Processing Strategy

### Recommended Batch Sizes

| Total Items | Batch Size | Number of Batches | Run Schedule |
|-------------|-----------|-------------------|--------------|
| 1-25 | 25 | 1 | Single run |
| 26-50 | 25 | 2 | Back-to-back or split |
| 51-100 | 25 | 4 | Multiple sessions |
| 100-200 | 25-50 | 4-8 | Daily runs over week |
| 200+ | 50 | Multiple | Overnight runs |

### Batch Execution Pattern

**Example: 100 Historical Cookbooks**

```bash
# Batch 1: German cookbooks (25 items)
python main/downloader.py batches/german_cookbooks.csv \
  --output_dir output/batch_01_german \
  --config config_optimized.json \
  --log-level INFO

# Review results, adjust failures

# Batch 2: French cookbooks (25 items)
python main/downloader.py batches/french_cookbooks.csv \
  --output_dir output/batch_02_french \
  --config config_optimized.json \
  --log-level INFO

# Continue pattern...
```

### Parallel Processing (Advanced)

For very large collections, consider parallel execution:

```powershell
# Terminal 1
python main/downloader.py batch_01.csv --output_dir output_01 --config config.json

# Terminal 2 (different provider focus)
python main/downloader.py batch_02.csv --output_dir output_02 --config config.json
```

**Caution:** Ensure different batches target different primary providers to avoid rate limiting.

---

## Performance Tuning

### Bottleneck Identification

**Common Bottlenecks:**

1. **IIIF Manifest Retries** (FIXED)
   - Symptom: Long pauses with "500 for https://iiif..." warnings
   - Solution: Enable `prefer_pdf_over_images: true`

2. **Slow Provider APIs**
   - Symptom: Extended wait times between "Searching on X" messages
   - Solution: Adjust provider hierarchy, increase delays if rate-limited

3. **Large Image Downloads**
   - Symptom: Many "Downloaded image X/Y" messages
   - Solution: Set `max_pages` limits or prefer PDFs

4. **Network Issues**
   - Symptom: Multiple retry attempts, timeouts
   - Solution: Increase timeouts, reduce max_attempts to fail faster

### Speed Optimization Checklist

- [ ] `prefer_pdf_over_images: true` enabled
- [ ] `max_attempts: 5` (not 10)
- [ ] Provider hierarchy optimized for your content
- [ ] Appropriate delays set (no rate limiting)
- [ ] `max_pages` limits set if needed
- [ ] Download strategy: `selected_only`

### Expected Download Times

**PDF Downloads:**
- Small (10-50 MB): 5-15 seconds
- Medium (50-150 MB): 15-45 seconds
- Large (150-300 MB): 45-120 seconds

**Image Downloads (per 100 pages):**
- Low resolution: 30-60 seconds
- High resolution: 60-180 seconds
- Full resolution: 120-300 seconds

**Search/Selection:**
- Per provider search: 1-5 seconds
- Metadata collection: 2-10 seconds
- Selection decision: <1 second

---

## Monitoring and Recovery

### Real-Time Monitoring

**Watch For:**

✅ **Good Signs:**
```
INFO - Found X item(s) on [provider]
INFO - Downloading selected item from [provider]
INFO - Downloaded [file]
INFO - Finished processing '[work]'
```

⚠️ **Warning Signs:**
```
WARNING - No items found for '[work]' on [provider]
WARNING - 500 for [URL]; sleeping Xs (attempt Y/10)
```

❌ **Error Signs:**
```
ERROR - Giving up after 10 attempts
ERROR - No items found across all enabled APIs
ERROR - Download budget exhausted
```

### Log Analysis

**PowerShell Commands:**

```powershell
# Count successful downloads
Select-String "Finished processing" download.log | Measure-Object

# Find all failures
Select-String "No items found across all" download.log

# Check which providers succeeded
Select-String "Downloading selected item from" download.log

# Monitor download progress
Get-Content download.log -Wait -Tail 50
```

### Failure Recovery Strategy

**Step 1: Identify Failures**
```powershell
# Review index.csv to see which items failed
Import-Csv output/index.csv | Where-Object { $_.status -eq "failed" }
```

**Step 2: Analyze Failure Patterns**
- All from one provider? (Provider issue)
- Similar titles? (Title formatting issue)
- Random distribution? (Normal failure rate)

**Step 3: Create Retry CSV**
```csv
Title,Creator,Date
"Adjusted Title 1","Creator",1599
"Adjusted Title 2","Creator",1601
```

**Step 4: Retry with Adjustments**
```bash
python main/downloader.py failures_retry.csv \
  --output_dir output_retry \
  --config config_adjusted.json \
  --log-level DEBUG
```

### Automated Monitoring Script

```powershell
# monitor_downloads.ps1
$logFile = "download.log"
$outputDir = "output"

while ($true) {
    $completed = (Select-String "Finished processing" $logFile).Count
    $failed = (Select-String "No items found across" $logFile).Count
    $total = $completed + $failed
    
    Write-Host "Progress: $completed completed, $failed failed (Total: $total)"
    
    Start-Sleep -Seconds 60
}
```

---

## Quality Control

### Post-Download Verification

**1. Quick Checks**

```powershell
# Count downloaded items
Get-ChildItem output -Directory | Measure-Object

# Check which have objects
Get-ChildItem output -Recurse -Directory -Filter "objects" | 
    ForEach-Object { $_.Parent.Name }

# Calculate total download size
Get-ChildItem output -Recurse | 
    Measure-Object -Property Length -Sum | 
    Select-Object @{Name="SizeGB";Expression={$_.Sum / 1GB}}
```

**2. Metadata Review**

```powershell
# Check all work.json files for completeness
Get-ChildItem output -Recurse -Filter "work.json" | 
    ForEach-Object {
        $work = Get-Content $_.FullName | ConvertFrom-Json
        [PSCustomObject]@{
            Title = $work.query.title
            Provider = $work.selected_provider
            Score = $work.match_score
        }
    }
```

**3. PDF Validation**

Sample check PDFs to ensure:
- ✅ Files open correctly
- ✅ Content matches expected work
- ✅ Complete (not truncated)
- ✅ Reasonable file size

**4. Success Rate Analysis**

```powershell
# Calculate success rate from index.csv
$data = Import-Csv output/index.csv
$success = ($data | Where-Object { $_.objects_downloaded -gt 0 }).Count
$total = $data.Count
$rate = [math]::Round(($success / $total) * 100, 2)

Write-Host "Success Rate: $rate% ($success/$total)"
```

### Common Quality Issues

**Issue: Wrong Work Downloaded**
- **Cause:** Title too generic, fuzzy match scored incorrectly
- **Fix:** Review work.json, check match_score, adjust CSV title

**Issue: Incomplete Downloads**
- **Cause:** Download limits exceeded, network interruption
- **Fix:** Check download_limits in config, re-run specific items

**Issue: Low-Quality PDFs**
- **Cause:** Source material quality varies
- **Fix:** Try alternate provider from metadata folder

---

## Storage Management

### Directory Structure

```
project_root/
├── output/
│   ├── batch_01_german/
│   │   ├── e_0001_work_name/
│   │   │   ├── metadata/          (~1-5 MB)
│   │   │   ├── objects/           (~100-500 MB)
│   │   │   └── work.json          (~10 KB)
│   │   └── index.csv
│   ├── batch_02_french/
│   └── ...
├── logs/
│   ├── batch_01_2024-10-02.log
│   └── ...
└── config/
    ├── config_german.json
    └── ...
```

### Archive Strategy

**After Completion:**

1. **Compress Metadata** (optional)
   ```powershell
   Compress-Archive -Path output/*/metadata -DestinationPath metadata_backup.zip
   ```

2. **Separate Objects by Type**
   ```powershell
   # Move all PDFs to dedicated folder
   Get-ChildItem output -Recurse -Filter "*.pdf" | 
       Copy-Item -Destination pdfs/
   ```

3. **Create Manifest**
   ```powershell
   # Export index files
   Get-ChildItem output -Recurse -Filter "index.csv" | 
       ForEach-Object { Import-Csv $_ } | 
       Export-Csv master_index.csv
   ```

### Disk Space Monitoring

```powershell
# Check available space before starting
Get-PSDrive C | Select-Object Used,Free

# Monitor during download
while ($true) {
    $size = (Get-ChildItem output -Recurse | 
        Measure-Object -Property Length -Sum).Sum / 1GB
    Write-Host "Current size: $([math]::Round($size, 2)) GB"
    Start-Sleep 300  # Check every 5 minutes
}
```

---

## Advanced Workflows

### Multi-Provider Fallback Strategy

**Scenario:** Primary provider often fails for your content type.

**Solution:** Configure multiple attempts with different provider hierarchies.

```bash
# Attempt 1: MDZ first
python main/downloader.py items.csv --config config_mdz_priority.json --output_dir attempt_01

# Attempt 2: Retry failures with Gallica priority
python main/downloader.py failures.csv --config config_gallica_priority.json --output_dir attempt_02
```

### Metadata-Only Collection Phase

**Scenario:** Want to review all candidates before downloading objects.

**Strategy:** Two-phase approach.

**Phase 1: Metadata Collection**
```json
{
  "selection": {
    "download_strategy": "metadata_only"
  }
}
```

**Phase 2: Selective Download**
After manual review, create a new CSV with adjusted titles and run full download.

### Incremental Updates

**Scenario:** Regularly add new items to collection.

**Strategy:**
1. Maintain master CSV with all items
2. Track downloaded items in database or spreadsheet
3. Create delta CSV with only new items
4. Run delta downloads to same output directory

```bash
python main/downloader.py new_items_2024-10.csv \
  --output_dir collection \
  --config config.json
```

---

## Troubleshooting Large-Scale Runs

### Issue: Process Crashes Mid-Run

**Recovery:**
1. Check which items completed (review index.csv)
2. Create CSV with remaining items
3. Resume from last successful item

### Issue: Rate Limited by Provider

**Symptoms:**
- 429 errors
- Repeated timeouts
- Very slow responses

**Solutions:**
- Increase `delay_ms` for that provider
- Reduce `max_candidates_per_provider`
- Split into smaller batches with time gaps
- Switch to different provider

### Issue: Inconsistent Results

**Causes:**
- Provider availability varies
- Fuzzy matching threshold too permissive
- Network conditions changing

**Solutions:**
- Run during off-peak hours
- Increase `min_title_score`
- Use more reliable providers (MDZ, Gallica)

---

## Best Practices Summary

### Configuration
- ✅ Always enable `prefer_pdf_over_images: true`
- ✅ Set `max_attempts: 5` (not 10)
- ✅ Configure appropriate provider delays
- ✅ Set reasonable download limits

### CSV Preparation
- ✅ Use original language titles
- ✅ Research exact titles in library catalogs
- ✅ Keep diacritics and special characters
- ✅ Test with small batch first

### Execution
- ✅ Start with 5-10 items to validate
- ✅ Use batch sizes of 25-50 items
- ✅ Monitor logs in real-time
- ✅ Run large batches overnight

### Quality Control
- ✅ Review index.csv after each batch
- ✅ Spot-check downloaded files
- ✅ Track success rates
- ✅ Adjust configuration based on results

### Recovery
- ✅ Identify failure patterns
- ✅ Adjust titles for failures
- ✅ Retry with modified configuration
- ✅ Document what works for your collection

---

## Conclusion

Large-scale downloading requires careful planning, proper configuration, and iterative refinement. The key to success is:

1. **Start small** - Validate before scaling
2. **Optimize configuration** - Use proven settings
3. **Monitor actively** - Catch issues early
4. **Iterate on failures** - Learn and adjust
5. **Document your process** - Build institutional knowledge

With these practices, you can achieve 60-70% success rates on large collections (50-200+ items) with minimal manual intervention.

For specific use cases (historical cookbooks, medical texts, etc.), see the specialized guides in the `cookbooks/` or other collection-specific directories.

Happy downloading! 🚀
