# Configuration Guide for ChronoDownloader

This guide explains how to configure `config.json` for optimal usage.

## Download Limits

Download limits have been restructured for easier configuration. Instead of API-specific byte limits, you now configure limits by **content type** in **GB units**.

### Structure

```json
"download_limits": {
  "total": {
    "images_gb": 100,      // Total GB allowed for downloaded images across all works
    "pdfs_gb": 50,         // Total GB allowed for downloaded PDFs across all works
    "metadata_gb": 1       // Total GB allowed for metadata files
  },
  "per_work": {
    "images_gb": 5,        // Max GB of images per individual work
    "pdfs_gb": 3,          // Max GB of PDFs per individual work
    "metadata_mb": 10      // Max MB of metadata per work (in MB for finer control)
  },
  "on_exceed": "stop"      // "stop" = halt downloads when limit reached
                           // "skip" = skip items that would exceed limit but continue
}
```

### Configuration Tips

1. **Images vs PDFs**: 
   - Images typically require more storage (one image per page)
   - PDFs are usually more compact (single file per work)
   - Allocate more space to images if downloading primarily scanned works

2. **Per-Work Limits**: 
   - Prevents any single work from consuming too much space
   - Useful for handling unexpectedly large works
   - Set to 0 or remove to disable per-work limits

3. **Total Limits**:
   - Sets the overall budget for your entire download session
   - When exceeded, behavior depends on `on_exceed` setting

4. **On Exceed Behavior**:
   - `"stop"`: Immediately halt all downloads when any limit is reached
   - `"skip"`: Skip the specific item that would exceed the limit, but continue with other downloads

### Example Configurations

#### Conservative (Small disk space)
```json
"download_limits": {
  "total": {
    "images_gb": 20,
    "pdfs_gb": 10,
    "metadata_gb": 0.5
  },
  "per_work": {
    "images_gb": 2,
    "pdfs_gb": 1,
    "metadata_mb": 5
  },
  "on_exceed": "stop"
}
```

#### Moderate (Balanced)
```json
"download_limits": {
  "total": {
    "images_gb": 100,
    "pdfs_gb": 50,
    "metadata_gb": 1
  },
  "per_work": {
    "images_gb": 5,
    "pdfs_gb": 3,
    "metadata_mb": 10
  },
  "on_exceed": "stop"
}
```

#### Generous (Large disk space)
```json
"download_limits": {
  "total": {
    "images_gb": 500,
    "pdfs_gb": 200,
    "metadata_gb": 5
  },
  "per_work": {
    "images_gb": 20,
    "pdfs_gb": 10,
    "metadata_mb": 50
  },
  "on_exceed": "skip"
}
```

## Other Important Settings

### Download Preferences

```json
"download": {
  "prefer_pdf_over_images": false,  // Set to true to prioritize PDF downloads
  "download_manifest_renderings": true,  // Download alternative formats from manifests
  "max_renderings_per_manifest": 5,
  "rendering_mime_whitelist": ["application/pdf", "application/epub+zip"],
  "overwrite_existing": false,  // Skip already downloaded files
  "include_metadata": true      // Download metadata JSON files
}
```

### Provider Selection

Enable or disable specific providers:

```json
"providers": {
  "internet_archive": true,
  "bnf_gallica": true,
  "google_books": true,
  // ... set to false to disable a provider
}
```

### Provider Hierarchy

Control which provider is preferred when multiple sources are available:

```json
"selection": {
  "provider_hierarchy": [
    "mdz",              // Try MDZ first
    "bnf_gallica",      // Then Gallica
    "google_books",     // Then Google Books
    "internet_archive"  // Finally Internet Archive
    // ... order matters!
  ]
}
```

## Need Help?

- Check the logs for detailed information about download progress and limit tracking
- The system will automatically log a summary of downloads by content type
- Adjust limits based on your storage capacity and download needs
