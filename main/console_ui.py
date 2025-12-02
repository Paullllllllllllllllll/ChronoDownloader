"""Console UI utilities and data classes for ChronoDownloader interactive mode.

This module provides reusable console UI components including styled output,
prompts, and the DownloadConfiguration dataclass for session configuration.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DownloadConfiguration:
    """User configuration for a download session."""
    
    # Mode: "csv", "single", "collection"
    mode: str = "csv"
    
    # CSV mode settings
    csv_path: str | None = None
    
    # Single work mode settings
    single_title: str | None = None
    single_creator: str | None = None
    single_entry_id: str | None = None
    
    # Collection mode settings
    collection_name: str | None = None
    
    # General settings
    output_dir: str = "downloaded_works"
    config_path: str = "config.json"
    dry_run: bool = False
    log_level: str = "INFO"
    
    # Provider overrides (optional)
    provider_hierarchy: list[str] = field(default_factory=list)
    
    # Selected items for processing
    selected_works: list[dict[str, Any]] = field(default_factory=list)


class ConsoleUI:
    """Simple console UI utilities with ANSI color support."""
    
    # ANSI color codes (Windows 10+ supports these)
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RED = "\033[91m"
    
    @staticmethod
    def enable_ansi() -> None:
        """Enable ANSI escape codes on Windows."""
        if sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                pass
    
    @staticmethod
    def print_header(title: str, subtitle: str = "") -> None:
        """Print a styled header."""
        width = 70
        print()
        print(f"{ConsoleUI.BOLD}{ConsoleUI.CYAN}{'=' * width}{ConsoleUI.RESET}")
        print(f"{ConsoleUI.BOLD}{ConsoleUI.CYAN}  {title}{ConsoleUI.RESET}")
        if subtitle:
            print(f"{ConsoleUI.DIM}  {subtitle}{ConsoleUI.RESET}")
        print(f"{ConsoleUI.BOLD}{ConsoleUI.CYAN}{'=' * width}{ConsoleUI.RESET}")
        print()
    
    @staticmethod
    def print_separator(char: str = "-", width: int = 70) -> None:
        """Print a separator line."""
        print(f"{ConsoleUI.DIM}{char * width}{ConsoleUI.RESET}")
    
    @staticmethod
    def print_info(label: str, message: str = "") -> None:
        """Print an info message."""
        if message:
            print(f"{ConsoleUI.BLUE}[{label}]{ConsoleUI.RESET} {message}")
        else:
            print(f"{ConsoleUI.BLUE}{label}{ConsoleUI.RESET}")
    
    @staticmethod
    def print_success(message: str) -> None:
        """Print a success message."""
        print(f"{ConsoleUI.GREEN}✓ {message}{ConsoleUI.RESET}")
    
    @staticmethod
    def print_warning(message: str) -> None:
        """Print a warning message."""
        print(f"{ConsoleUI.YELLOW}⚠ {message}{ConsoleUI.RESET}")
    
    @staticmethod
    def print_error(message: str) -> None:
        """Print an error message."""
        print(f"{ConsoleUI.RED}✗ {message}{ConsoleUI.RESET}")
    
    @staticmethod
    def prompt_select(
        question: str,
        options: list[tuple[str, str]],
        allow_back: bool = True
    ) -> str | None:
        """Prompt user to select from options.
        
        Args:
            question: Question to display
            options: List of (value, display_text) tuples
            allow_back: Whether to allow going back
            
        Returns:
            Selected value or None if user wants to go back/quit
        """
        print(f"\n{ConsoleUI.BOLD}{question}{ConsoleUI.RESET}\n")
        
        for i, (value, display) in enumerate(options, 1):
            print(f"  {ConsoleUI.CYAN}[{i}]{ConsoleUI.RESET} {display}")
        
        if allow_back:
            print(f"\n  {ConsoleUI.DIM}[b] Go back  [q] Quit{ConsoleUI.RESET}")
        else:
            print(f"\n  {ConsoleUI.DIM}[q] Quit{ConsoleUI.RESET}")
        
        while True:
            try:
                choice = input(f"\n{ConsoleUI.BOLD}Enter choice: {ConsoleUI.RESET}").strip().lower()
                
                if choice == "q":
                    raise KeyboardInterrupt()
                if choice == "b" and allow_back:
                    return None
                
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(options):
                        return options[idx][0]
                except ValueError:
                    pass
                
                print(f"{ConsoleUI.YELLOW}Invalid choice. Please try again.{ConsoleUI.RESET}")
                
            except EOFError:
                raise KeyboardInterrupt()
    
    @staticmethod
    def prompt_input(
        prompt: str,
        default: str = "",
        required: bool = False
    ) -> str:
        """Prompt for text input.
        
        Args:
            prompt: Prompt text
            default: Default value if empty
            required: Whether input is required
            
        Returns:
            User input or default
        """
        default_hint = f" [{default}]" if default else ""
        required_hint = " (required)" if required else ""
        
        while True:
            try:
                value = input(f"{ConsoleUI.BOLD}{prompt}{default_hint}{required_hint}: {ConsoleUI.RESET}").strip()
                
                if not value and default:
                    return default
                if not value and required:
                    print(f"{ConsoleUI.YELLOW}This field is required.{ConsoleUI.RESET}")
                    continue
                return value
                
            except EOFError:
                raise KeyboardInterrupt()
    
    @staticmethod
    def prompt_yes_no(question: str, default: bool = True) -> bool:
        """Prompt for yes/no confirmation.
        
        Args:
            question: Question to ask
            default: Default value
            
        Returns:
            True for yes, False for no
        """
        hint = "[Y/n]" if default else "[y/N]"
        
        while True:
            try:
                answer = input(f"{ConsoleUI.BOLD}{question} {hint}: {ConsoleUI.RESET}").strip().lower()
                
                if not answer:
                    return default
                if answer in ("y", "yes"):
                    return True
                if answer in ("n", "no"):
                    return False
                
                print(f"{ConsoleUI.YELLOW}Please enter 'y' or 'n'.{ConsoleUI.RESET}")
                
            except EOFError:
                raise KeyboardInterrupt()


__all__ = ["ConsoleUI", "DownloadConfiguration"]
