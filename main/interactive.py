"""Interactive mode workflow for ChronoDownloader.

This module provides interactive prompts and workflows for users who want
to configure and run downloads without using command-line arguments.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

# Ensure parent directory is in path for direct script execution
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from api.core.config import get_config, get_download_config
from api.providers import PROVIDERS
from main import pipeline
from main.console_ui import ConsoleUI, DownloadConfiguration
from main.mode_selector import get_general_config

logger = logging.getLogger(__name__)


# =============================================================================
# Interactive Workflow
# =============================================================================

class InteractiveWorkflow:
    """Interactive workflow for configuring and running downloads."""
    
    def __init__(self):
        """Initialize the workflow."""
        ConsoleUI.enable_ansi()
        self.config = DownloadConfiguration()
        self.app_config: Dict[str, Any] = {}
    
    def display_welcome(self) -> None:
        """Display welcome banner."""
        ConsoleUI.print_header(
            "CHRONO DOWNLOADER",
            "Historical Document Download Tool"
        )
        print("  Download historical sources from digital libraries including:")
        print("  Internet Archive, BnF Gallica, MDZ, Google Books, and more.\n")
    
    def display_provider_status(self) -> None:
        """Display status of available providers."""
        ConsoleUI.print_separator()
        ConsoleUI.print_info("ENABLED PROVIDERS")
        ConsoleUI.print_separator()
        
        cfg = get_config()
        providers_cfg = cfg.get("providers", {})
        
        enabled = []
        disabled = []
        
        for key, (_, _, name) in PROVIDERS.items():
            if providers_cfg.get(key, False):
                enabled.append(name)
            else:
                disabled.append(name)
        
        if enabled:
            print(f"  {ConsoleUI.GREEN}Enabled:{ConsoleUI.RESET} {', '.join(enabled)}")
        if disabled:
            print(f"  {ConsoleUI.DIM}Disabled: {', '.join(disabled)}{ConsoleUI.RESET}")
        print()
    
    def get_mode_options(self) -> List[Tuple[str, str]]:
        """Get download mode options."""
        return [
            ("csv", "CSV Batch — Process works from a CSV file"),
            ("single", "Single Work — Download a specific work by title"),
            ("collection", "Predefined Collection — Choose from sample datasets"),
        ]
    
    def configure_mode(self) -> bool:
        """Configure download mode.
        
        Returns:
            True if configured, False to go back
        """
        result = ConsoleUI.prompt_select(
            "How would you like to specify works to download?",
            self.get_mode_options(),
            allow_back=False
        )
        
        if result:
            self.config.mode = result
            return True
        return False
    
    def configure_csv_mode(self) -> bool:
        """Configure CSV batch mode.
        
        Returns:
            True if configured, False to go back
        """
        general = get_general_config()
        default_csv = general.get("default_csv_path", "sample_works.csv")
        
        # List available CSV files
        csv_files = list(Path(".").glob("*.csv"))
        
        if csv_files:
            ConsoleUI.print_info("Available CSV files:")
            for f in csv_files:
                print(f"    • {f.name}")
            print()
        
        csv_path = ConsoleUI.prompt_input(
            "Enter path to CSV file",
            default=default_csv,
            required=True
        )
        
        if not csv_path:
            return False
        
        # Validate CSV exists
        if not os.path.exists(csv_path):
            ConsoleUI.print_error(f"CSV file not found: {csv_path}")
            return False
        
        # Validate CSV has required columns
        try:
            df = pd.read_csv(csv_path)
            
            # Apply column mappings
            cfg = get_config()
            mappings = cfg.get("csv_column_mapping", {})
            for target_col, source_candidates in mappings.items():
                if not isinstance(source_candidates, list):
                    source_candidates = [source_candidates]
                for source_col in source_candidates:
                    if source_col in df.columns:
                        if target_col == "title" and "Title" not in df.columns:
                            df.rename(columns={source_col: "Title"}, inplace=True)
                        break
            
            if "Title" not in df.columns:
                ConsoleUI.print_error("CSV must have a 'Title' column (or mapped equivalent)")
                return False
            
            ConsoleUI.print_success(f"Found {len(df)} works in CSV")
            self.config.csv_path = csv_path
            return True
            
        except Exception as e:
            ConsoleUI.print_error(f"Error reading CSV: {e}")
            return False
    
    def configure_single_mode(self) -> bool:
        """Configure single work mode.
        
        Returns:
            True if configured, False to go back
        """
        ConsoleUI.print_info("SINGLE WORK DOWNLOAD")
        print("  Enter details for the work you want to download.\n")
        
        title = ConsoleUI.prompt_input("Work title", required=True)
        if not title:
            return False
        
        creator = ConsoleUI.prompt_input("Creator/Author (optional)")
        entry_id = ConsoleUI.prompt_input("Entry ID (optional)", default="W0001")
        
        self.config.single_title = title
        self.config.single_creator = creator if creator else None
        self.config.single_entry_id = entry_id if entry_id else "W0001"
        
        return True
    
    def configure_collection_mode(self) -> bool:
        """Configure predefined collection mode.
        
        Returns:
            True if configured, False to go back
        """
        # Look for predefined collections (CSV files with special names or in a collections folder)
        collections = []
        
        # Check root directory for sample files
        for csv_file in Path(".").glob("*.csv"):
            name = csv_file.stem
            try:
                df = pd.read_csv(csv_file)
                count = len(df)
                collections.append((str(csv_file), f"{name} ({count} works)"))
            except Exception:
                continue
        
        # Check for collections directory
        collections_dir = Path("collections")
        if collections_dir.exists():
            for csv_file in collections_dir.glob("*.csv"):
                name = csv_file.stem
                try:
                    df = pd.read_csv(csv_file)
                    count = len(df)
                    collections.append((str(csv_file), f"{name} ({count} works)"))
                except Exception:
                    continue
        
        if not collections:
            ConsoleUI.print_warning("No predefined collections found.")
            ConsoleUI.print_info("Place CSV files in the current directory or 'collections/' folder.")
            return False
        
        result = ConsoleUI.prompt_select(
            "Select a collection to download:",
            collections,
            allow_back=True
        )
        
        if result:
            self.config.collection_name = result
            self.config.csv_path = result
            return True
        return False
    
    def configure_output(self) -> bool:
        """Configure output directory.
        
        Returns:
            True if configured, False to go back
        """
        general = get_general_config()
        default_output = general.get("default_output_dir", "downloaded_works")
        
        output_dir = ConsoleUI.prompt_input(
            "Output directory",
            default=default_output
        )
        
        if not output_dir:
            output_dir = default_output
        
        self.config.output_dir = output_dir
        return True
    
    def configure_options(self) -> bool:
        """Configure additional options.
        
        Returns:
            True if configured, False to go back
        """
        ConsoleUI.print_info("ADDITIONAL OPTIONS")
        print()
        
        # Dry run option
        self.config.dry_run = ConsoleUI.prompt_yes_no(
            "Dry run (search only, no downloads)?",
            default=False
        )
        
        # Parallel download options (only for batch modes)
        if self.config.mode in ("csv", "collection"):
            dl_config = get_download_config()
            config_max_parallel = int(dl_config.get("max_parallel_downloads", 1) or 1)
            
            if config_max_parallel > 1:
                # Config already enables parallelism
                ConsoleUI.print_info(
                    f"Parallel downloads enabled via config ({config_max_parallel} workers)"
                )
                self.config.use_parallel = True
                self.config.max_workers_override = None
            else:
                # Ask if user wants to enable parallelism
                if ConsoleUI.prompt_yes_no("Enable parallel downloads?", default=True):
                    self.config.use_parallel = True
                    workers_input = ConsoleUI.prompt_input(
                        "Number of parallel workers",
                        default="4"
                    )
                    try:
                        workers = max(1, int(workers_input))
                        self.config.max_workers_override = workers
                    except ValueError:
                        self.config.max_workers_override = 4
                else:
                    self.config.use_parallel = False
                    self.config.max_workers_override = None
        
        # Log level
        log_options = [
            ("INFO", "INFO — Standard logging (recommended)"),
            ("DEBUG", "DEBUG — Verbose logging for troubleshooting"),
            ("WARNING", "WARNING — Only show warnings and errors"),
        ]
        
        result = ConsoleUI.prompt_select(
            "Select logging level:",
            log_options,
            allow_back=True
        )
        
        if result:
            self.config.log_level = result
            return True
        return False
    
    def display_summary(self) -> bool:
        """Display configuration summary and confirm.
        
        Returns:
            True to proceed, False to go back
        """
        ConsoleUI.print_header("CONFIGURATION SUMMARY", "Review your settings")
        
        print(f"  {ConsoleUI.BOLD}Mode:{ConsoleUI.RESET} {self.config.mode.upper()}")
        
        if self.config.mode == "csv":
            print(f"  {ConsoleUI.BOLD}CSV File:{ConsoleUI.RESET} {self.config.csv_path}")
        elif self.config.mode == "single":
            print(f"  {ConsoleUI.BOLD}Title:{ConsoleUI.RESET} {self.config.single_title}")
            if self.config.single_creator:
                print(f"  {ConsoleUI.BOLD}Creator:{ConsoleUI.RESET} {self.config.single_creator}")
        elif self.config.mode == "collection":
            print(f"  {ConsoleUI.BOLD}Collection:{ConsoleUI.RESET} {self.config.collection_name}")
        
        print(f"  {ConsoleUI.BOLD}Output Directory:{ConsoleUI.RESET} {self.config.output_dir}")
        print(f"  {ConsoleUI.BOLD}Dry Run:{ConsoleUI.RESET} {'Yes' if self.config.dry_run else 'No'}")
        
        # Show parallel settings for batch modes
        if self.config.mode in ("csv", "collection"):
            if self.config.use_parallel and not self.config.dry_run:
                workers = self.config.max_workers_override
                if workers:
                    print(f"  {ConsoleUI.BOLD}Parallel Downloads:{ConsoleUI.RESET} {workers} workers")
                else:
                    dl_config = get_download_config()
                    workers = int(dl_config.get("max_parallel_downloads", 1) or 1)
                    print(f"  {ConsoleUI.BOLD}Parallel Downloads:{ConsoleUI.RESET} {workers} workers (from config)")
            else:
                print(f"  {ConsoleUI.BOLD}Parallel Downloads:{ConsoleUI.RESET} Sequential")
        
        print(f"  {ConsoleUI.BOLD}Log Level:{ConsoleUI.RESET} {self.config.log_level}")
        
        print()
        return ConsoleUI.prompt_yes_no("Proceed with these settings?", default=True)
    
    def run_workflow(self) -> Optional[DownloadConfiguration]:
        """Run the interactive configuration workflow.
        
        Returns:
            DownloadConfiguration if completed, None if cancelled
        """
        self.display_welcome()
        self.display_provider_status()
        
        # State machine for navigation
        current_step = "mode"
        
        while True:
            try:
                if current_step == "mode":
                    if self.configure_mode():
                        current_step = f"configure_{self.config.mode}"
                    else:
                        return None
                
                elif current_step == "configure_csv":
                    if self.configure_csv_mode():
                        current_step = "output"
                    else:
                        current_step = "mode"
                
                elif current_step == "configure_single":
                    if self.configure_single_mode():
                        current_step = "output"
                    else:
                        current_step = "mode"
                
                elif current_step == "configure_collection":
                    if self.configure_collection_mode():
                        current_step = "output"
                    else:
                        current_step = "mode"
                
                elif current_step == "output":
                    if self.configure_output():
                        current_step = "options"
                    else:
                        current_step = f"configure_{self.config.mode}"
                
                elif current_step == "options":
                    if self.configure_options():
                        current_step = "summary"
                    else:
                        current_step = "output"
                
                elif current_step == "summary":
                    if self.display_summary():
                        return self.config
                    else:
                        current_step = "options"
                
            except KeyboardInterrupt:
                print(f"\n{ConsoleUI.YELLOW}Operation cancelled.{ConsoleUI.RESET}")
                return None


# =============================================================================
# Processing Functions
# =============================================================================

def _normalize_csv_columns(df: pd.DataFrame, config_path: str, log: logging.Logger) -> pd.DataFrame:
    """Normalize CSV column names based on configured mappings.
    
    Args:
        df: Input DataFrame
        config_path: Path to configuration JSON file
        log: Logger instance
        
    Returns:
        DataFrame with normalized column names
    """
    import json
    
    # Try to load column mappings from config
    mappings = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                mappings = cfg.get("csv_column_mapping", {})
        except Exception as e:
            log.warning("Could not load csv_column_mapping from config: %s", e)
    
    # Apply mappings: for each target column, find first matching source column
    for target_col, source_candidates in mappings.items():
        if not isinstance(source_candidates, list):
            source_candidates = [source_candidates]
        
        # Check if target already exists
        if target_col.capitalize() in df.columns:
            continue
            
        # Find first matching source column
        for source_col in source_candidates:
            if source_col in df.columns:
                # Rename to standard format (Title, Creator, entry_id)
                if target_col == "title":
                    df.rename(columns={source_col: "Title"}, inplace=True)
                    log.info("Mapped column '%s' to 'Title'", source_col)
                elif target_col == "creator":
                    df.rename(columns={source_col: "Creator"}, inplace=True)
                    log.info("Mapped column '%s' to 'Creator'", source_col)
                elif target_col == "entry_id":
                    df.rename(columns={source_col: "entry_id"}, inplace=True)
                    log.info("Mapped column '%s' to 'entry_id'", source_col)
                break
    
    return df


def process_csv_batch(
    csv_path: str,
    output_dir: str,
    config_path: str,
    dry_run: bool,
    log: logging.Logger,
    use_parallel: bool = True,
    max_workers_override: Optional[int] = None
) -> int:
    """Process works from a CSV file.
    
    Supports both parallel and sequential processing modes.
    
    Args:
        csv_path: Path to CSV file
        output_dir: Output directory
        config_path: Path to config file
        dry_run: Whether to skip downloads
        log: Logger instance
        use_parallel: Whether to use parallel downloads
        max_workers_override: Override for max_parallel_downloads config
        
    Returns:
        Number of works processed
    """
    from main.execution import run_batch_downloads, create_interactive_callbacks
    
    # Load and validate CSV
    if not os.path.exists(csv_path):
        log.error("CSV file not found at %s", csv_path)
        return 0
    
    try:
        works_df = pd.read_csv(csv_path)
    except Exception as e:
        log.error("Error reading CSV file: %s", e)
        return 0
    
    # Apply column mappings from config
    works_df = _normalize_csv_columns(works_df, config_path, log)
    
    if "Title" not in works_df.columns:
        log.error("CSV file must contain a 'Title' column or a mapped equivalent.")
        return 0
    
    log.info("Starting downloader. Output directory: %s", output_dir)
    log.info("Works to process: %d", len(works_df))
    
    # Get config for execution
    config = get_config()
    
    # Create interactive-friendly callbacks for parallel mode
    on_submit, on_complete = create_interactive_callbacks(log)
    
    # Use shared execution module for both parallel and sequential
    stats = run_batch_downloads(
        works_df=works_df,
        output_dir=output_dir,
        config=config,
        dry_run=dry_run,
        use_parallel=use_parallel,
        max_workers_override=max_workers_override,
        logger=log,
        on_submit=on_submit,
        on_complete=on_complete,
    )
    
    return stats.get("processed", 0)


def process_single_work(
    title: str,
    creator: Optional[str],
    entry_id: str,
    output_dir: str,
    dry_run: bool,
    log: logging.Logger
) -> bool:
    """Process a single work.
    
    Args:
        title: Work title
        creator: Optional creator name
        entry_id: Entry identifier
        output_dir: Output directory
        dry_run: Whether to skip downloads
        log: Logger instance
        
    Returns:
        True if processed successfully
    """
    log.info("Processing single work: '%s'%s", title, f" by '{creator}'" if creator else "")
    
    pipeline.process_work(
        title,
        creator,
        entry_id,
        output_dir,
        dry_run=dry_run,
    )
    
    return True


def run_interactive_session(config: DownloadConfiguration) -> None:
    """Execute download session based on interactive configuration.
    
    Args:
        config: DownloadConfiguration from interactive workflow
    """
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    
    # Reduce noisy retry logs
    try:
        logging.getLogger("urllib3").setLevel(logging.ERROR)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    except Exception:
        pass
    
    log = logging.getLogger(__name__)
    
    # Set config path
    os.environ["CHRONO_CONFIG_PATH"] = config.config_path
    
    # Load and validate providers
    providers = pipeline.load_enabled_apis(config.config_path)
    providers = pipeline.filter_enabled_providers_for_keys(providers)
    pipeline.ENABLED_APIS = providers
    
    if not providers:
        log.warning("No providers are enabled. Update %s to enable providers.", config.config_path)
        ConsoleUI.print_error("No providers are enabled. Check your configuration.")
        return
    
    ConsoleUI.print_header("DOWNLOAD IN PROGRESS", "Please wait...")
    
    processed = 0
    
    if config.mode == "csv" or config.mode == "collection":
        processed = process_csv_batch(
            config.csv_path or "",
            config.output_dir,
            config.config_path,
            config.dry_run,
            log,
            use_parallel=config.use_parallel,
            max_workers_override=config.max_workers_override,
        )
    elif config.mode == "single":
        if process_single_work(
            config.single_title or "",
            config.single_creator,
            config.single_entry_id or "W0001",
            config.output_dir,
            config.dry_run,
            log
        ):
            processed = 1
    
    # Handle deferred downloads
    deferred = pipeline.get_deferred_downloads()
    if deferred:
        log.info(
            "%d download(s) were deferred due to quota limits.",
            len(deferred)
        )
        if ConsoleUI.prompt_yes_no("Process deferred downloads now?", default=False):
            pipeline.process_deferred_downloads(wait_for_reset=True)
    
    # Display completion summary
    print()
    ConsoleUI.print_separator("=")
    ConsoleUI.print_success(f"Download session complete!")
    print(f"  Works processed: {processed}")
    print(f"  Output directory: {config.output_dir}")
    if config.dry_run:
        print(f"  {ConsoleUI.YELLOW}(Dry run - no files downloaded){ConsoleUI.RESET}")
    ConsoleUI.print_separator("=")


def run_interactive() -> None:
    """Main entry point for interactive mode."""
    workflow = InteractiveWorkflow()
    config = workflow.run_workflow()
    
    if config:
        run_interactive_session(config)
    else:
        print("\nDownload cancelled.")
