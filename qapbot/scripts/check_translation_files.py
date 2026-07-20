#!/usr/bin/env python3
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""
Translation file checker script.

This script performs comprehensive analysis of translation files:
1. Compares en.json and de.json key counts
2. Verifies all keys exist in both files
3. Searches the codebase for unused translation keys
4. Reports findings with suggestions for cleanup

Usage:
    python qapbot/scripts/check_translation_files.py
"""

import json
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple, Any
from collections import defaultdict


class TranslationChecker:
    """Analyzes translation files and codebase for consistency and unused keys."""
    
    def __init__(self):
        """Initialize the checker with paths."""
        self.script_dir = Path(__file__).parent
        self.project_root = self.script_dir.parent.parent
        self.en_path = self.project_root / "qapbot" / "translations" / "en.json"
        self.de_path = self.project_root / "qapbot" / "translations" / "de.json"
        
        self.en_data: Dict[str, Any] = {}
        self.de_data: Dict[str, Any] = {}
        self.en_keys: Set[str] = set()
        self.de_keys: Set[str] = set()
        
    def load_files(self) -> bool:
        """Load both translation files."""
        try:
            with open(self.en_path, 'r', encoding='utf-8') as f:
                self.en_data = json.load(f)
            with open(self.de_path, 'r', encoding='utf-8') as f:
                self.de_data = json.load(f)
            
            self._extract_all_keys(self.en_data, "", self.en_keys)
            self._extract_all_keys(self.de_data, "", self.de_keys)
            
            return True
        except Exception as e:
            print(f"❌ Error loading translation files: {e}")
            return False
    
    def _extract_all_keys(self, obj: Dict[str, Any], prefix: str, keys_set: Set[str]) -> None:
        """Recursively extract all dot-notation keys from nested dict."""
        for key, value in obj.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                self._extract_all_keys(value, full_key, keys_set)
            else:
                keys_set.add(full_key)
    
    def check_key_counts(self) -> Tuple[bool, str]:
        """Check if both files have the same number of keys."""
        en_count = len(self.en_keys)
        de_count = len(self.de_keys)
        
        status = "✅" if en_count == de_count else "❌"
        message = f"{status} Key Count Comparison:\n"
        message += f"   en.json: {en_count} keys\n"
        message += f"   de.json: {de_count} keys"
        
        return en_count == de_count, message
    
    def check_key_consistency(self) -> Tuple[bool, str]:
        """Check if all keys exist in both files."""
        en_only = self.en_keys - self.de_keys
        de_only = self.de_keys - self.en_keys
        
        all_consistent = len(en_only) == 0 and len(de_only) == 0
        
        message = f"{'✅' if all_consistent else '❌'} Key Consistency Check:\n"
        
        if en_only:
            message += f"   ⚠️  Keys in en.json but NOT in de.json ({len(en_only)}):\n"
            for key in sorted(en_only):
                message += f"      • {key}\n"
        
        if de_only:
            message += f"   ⚠️  Keys in de.json but NOT in en.json ({len(de_only)}):\n"
            for key in sorted(de_only):
                message += f"      • {key}\n"
        
        if all_consistent:
            message += "   All keys are present in both files ✓"
        
        return all_consistent, message
    
    def find_unused_keys(self) -> Tuple[List[str], str]:
        """Search codebase for unused translation keys."""
        unused_keys = []
        code_files = self._get_python_files()
        
        print("\n🔍 Searching codebase for key usage...")
        print(f"   Scanning {len(code_files)} Python files...")
        
        for key in sorted(self.en_keys):
            # Skip _meta keys - these are metadata and not used in code
            if key.startswith("_meta."):
                continue
            
            # Build regex to find t('key', ...) or t("key", ...)
            # Escape dots in the key for regex
            escaped_key = re.escape(key)
            # Look for t('key') or t("key") with optional parameters
            pattern = rf"t\(['\"]({escaped_key})['\"]"
            
            found = False
            for code_file in code_files:
                try:
                    content = code_file.read_text(encoding='utf-8')
                    if re.search(pattern, content):
                        found = True
                        break
                except Exception:
                    # Skip files we can't read
                    pass
            
            if not found:
                unused_keys.append(key)
        
        if unused_keys:
            message = f"❌ Found {len(unused_keys)} UNUSED translation keys:\n\n"
            
            # Group by namespace for easier reading
            grouped = self._group_by_namespace(unused_keys)
            for namespace in sorted(grouped.keys()):
                message += f"   📌 {namespace}:\n"
                for key in sorted(grouped[namespace]):
                    short_key = key.replace(f"{namespace}.", "")
                    message += f"      • {short_key}\n"
        else:
            message = f"✅ All {len(self.en_keys)} translation keys are being used!"
        
        return unused_keys, message
    
    def _group_by_namespace(self, keys: List[str]) -> Dict[str, List[str]]:
        """Group keys by their top-level namespace."""
        grouped = defaultdict(list)
        for key in keys:
            namespace = key.split('.')[0]
            grouped[namespace].append(key)
        return grouped
    
    def _get_python_files(self) -> List[Path]:
        """Get all Python files in the project (excluding venv and __pycache__)."""
        exclude_dirs = {'venv', '__pycache__', '.git', 'data'}
        python_files = []
        
        for py_file in self.project_root.rglob("*.py"):
            # Skip if in excluded directory
            if any(excluded in py_file.parts for excluded in exclude_dirs):
                continue
            python_files.append(py_file)
        
        return python_files
    
    def run_full_check(self) -> int:
        """Run all checks and print results."""
        print("=" * 70)
        print("🔧 TRANSLATION FILE CHECKER")
        print("=" * 70)
        
        # Load files
        if not self.load_files():
            return 1
        
        print(f"\n📂 Files found:")
        print(f"   en.json: {self.en_path}")
        print(f"   de.json: {self.de_path}")
        
        # Check 1: Key counts
        print("\n" + "=" * 70)
        print("CHECK 1: KEY COUNT COMPARISON")
        print("=" * 70)
        count_ok, count_msg = self.check_key_counts()
        print(count_msg)
        
        # Check 2: Key consistency
        print("\n" + "=" * 70)
        print("CHECK 2: KEY CONSISTENCY")
        print("=" * 70)
        consistent_ok, consistency_msg = self.check_key_consistency()
        print(consistency_msg)
        
        # Check 3: Unused keys
        print("\n" + "=" * 70)
        print("CHECK 3: UNUSED KEYS")
        print("=" * 70)
        unused_keys, unused_msg = self.find_unused_keys()
        print(unused_msg)
        
        # Summary
        print("\n" + "=" * 70)
        print("📊 SUMMARY")
        print("=" * 70)
        
        all_ok = count_ok and consistent_ok and len(unused_keys) == 0
        
        if all_ok:
            print("✅ All checks passed! Translation files are healthy.")
            return 0
        else:
            issues = []
            if not count_ok:
                issues.append("Key count mismatch")
            if not consistent_ok:
                issues.append("Key consistency issues")
            if unused_keys:
                issues.append(f"{len(unused_keys)} unused keys")
            
            print(f"⚠️  Issues found: {', '.join(issues)}")
            print("\n💡 Recommendations:")
            
            if not count_ok or not consistent_ok:
                print("   1. Ensure all keys are present in both en.json and de.json")
                print("   2. Run this script again after fixing key inconsistencies")
            
            if unused_keys:
                print(f"   3. Consider removing {len(unused_keys)} unused keys:")
                print(f"      - Search codebase manually to confirm they're truly unused")
                print(f"      - Remove from both en.json and de.json")
                print(f"      - Re-run this script to verify cleanup")
            
            return 1


def main():
    """Main entry point."""
    checker = TranslationChecker()
    return checker.run_full_check()


if __name__ == "__main__":
    exit(main())
