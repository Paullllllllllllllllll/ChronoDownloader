"""Centralized mode selector for dual CLI/interactive script execution.

This module provides a unified interface for all main scripts to route execution
based on the interactive_mode configuration flag, eliminating code duplication.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

# Ensure parent directory is in path for direct script execution
if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from api.core.config import get_config

logger = logging.getLogger(__name__)


def _detect_mode_and_parse_args(
    parser_factory: Callable[[], argparse.ArgumentParser],
    script_name: str,
    config_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool, Optional[argparse.Namespace]]:
    """Internal helper that performs mode detection and argument parsing.
    
    This function handles:
    - Configuration loading
    - Mode detection (interactive vs CLI)
    - Argument parsing for CLI mode
    - Error handling for config loading failures
    
    Args:
        parser_factory: Function that returns an ArgumentParser
        script_name: Name of the calling script (for error messages)
        config_path: Optional path to config file (overrides CHRONO_CONFIG_PATH)
        
    Returns:
        Tuple of (config_dict, interactive_mode, args_or_none)
        
    Raises:
        SystemExit: On configuration loading failure
    """
    # Set config path if provided
    if config_path:
        os.environ["CHRONO_CONFIG_PATH"] = config_path
    
    try:
        config = get_config(force_reload=True)
    except Exception as e:
        logger.critical("%s: Failed to load configurations: %s", script_name, e)
        print(f"Error: Failed to load configurations: {e}")
        sys.exit(1)
    
    # Check for interactive mode in config
    general = config.get("general", {})
    interactive_mode = general.get("interactive_mode", True)
    
    args = None
    if not interactive_mode:
        # CLI mode: parse arguments
        parser = parser_factory()
        args = parser.parse_args()
        
        # Override config path if specified in CLI args
        if hasattr(args, 'config') and args.config:
            os.environ["CHRONO_CONFIG_PATH"] = args.config
            config = get_config(force_reload=True)
    else:
        # Interactive mode: check for --cli flag to force CLI mode
        # Do a quick pre-parse to check for override flags
        pre_parser = argparse.ArgumentParser(add_help=False)
        pre_parser.add_argument("--cli", action="store_true", help="Force CLI mode")
        pre_parser.add_argument("--interactive", action="store_true", help="Force interactive mode")
        pre_args, _ = pre_parser.parse_known_args()
        
        if pre_args.cli:
            interactive_mode = False
            parser = parser_factory()
            args = parser.parse_args()
            if hasattr(args, 'config') and args.config:
                os.environ["CHRONO_CONFIG_PATH"] = args.config
                config = get_config(force_reload=True)
    
    return config, interactive_mode, args


def run_with_mode_detection(
    interactive_handler: Callable[[], None],
    cli_handler: Callable[[argparse.Namespace, Dict[str, Any]], None],
    parser_factory: Callable[[], argparse.ArgumentParser],
    script_name: str,
    config_path: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool, Optional[argparse.Namespace]]:
    """Route execution based on interactive_mode configuration.
    
    Args:
        interactive_handler: Function to call in interactive mode (for type hints)
        cli_handler: Function to call in CLI mode (for type hints)
        parser_factory: Function that returns an ArgumentParser
        script_name: Name of the calling script (for error messages)
        config_path: Optional path to config file
        
    Returns:
        Tuple of (config_dict, interactive_mode, args_or_none)
        
    Raises:
        SystemExit: On configuration loading failure
    """
    return _detect_mode_and_parse_args(parser_factory, script_name, config_path)


def get_general_config() -> Dict[str, Any]:
    """Get general configuration section with defaults.
    
    Returns:
        Dictionary with general settings
    """
    cfg = get_config()
    gen = dict(cfg.get("general", {}) or {})
    
    # Defaults
    gen.setdefault("interactive_mode", True)
    gen.setdefault("default_output_dir", "downloaded_works")
    gen.setdefault("default_csv_path", "sample_works.csv")
    
    return gen
