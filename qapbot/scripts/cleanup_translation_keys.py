#!/usr/bin/env python3
# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false, reportUnknownVariableType=false
"""
Remove unused translation keys from en.json and de.json.

This script removes the 229 unused keys that were identified by the
check_translation_files.py script.
"""

import json
from pathlib import Path
from typing import Dict, Any, List

# All unused keys identified by the checker
UNUSED_KEYS: Dict[str, List[str]] = {
# Add the unused keys here!
}


def remove_key_from_dict(obj: Dict[str, Any], dot_path: str) -> bool:
    """
    Remove a key from a nested dict using dot notation.
    Returns True if key was found and removed.
    """
    parts = dot_path.split(".")
    current: Any = obj
    
    # Navigate to the parent of the target key
    for part in parts[:-1]:
        if part not in current:
            return False
        current = current[part]
        if not isinstance(current, dict):
            return False
    
    # Remove the final key
    final_key = parts[-1]
    if final_key in current:
        del current[final_key]
        return True
    return False


def clean_translation_file(filepath: Path) -> int:
    """Remove all unused keys from a translation file."""
    print(f"\n📝 Processing: {filepath.name}")
    
    # Load the file
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    removed_count = 0
    not_found_count = 0
    
    # Remove each unused key
    for namespace, keys in UNUSED_KEYS.items():
        for key in keys:
            # Build full dot path
            if namespace == "_meta":
                full_key = key  # Top-level key
            else:
                full_key = f"{namespace}.{key}"
            
            if remove_key_from_dict(data, full_key):
                removed_count += 1
            else:
                not_found_count += 1
    
    # Save back to file
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    print(f"   ✅ Removed: {removed_count} keys")
    if not_found_count > 0:
        print(f"   ⚠️  Not found: {not_found_count} keys (may have already been removed)")
    
    return removed_count


def main():
    project_root = Path(__file__).parent.parent.parent
    en_path = project_root / "qapbot" / "translations" / "en.json"
    de_path = project_root / "qapbot" / "translations" / "de.json"
    
    print("=" * 70)
    print("🧹 CLEANING UP UNUSED TRANSLATION KEYS")
    print("=" * 70)
    
    total_unused = sum(len(keys) for keys in UNUSED_KEYS.values())
    print(f"\n📊 Total keys to remove: {total_unused}")
    
    # Process both files
    en_removed = clean_translation_file(en_path)
    de_removed = clean_translation_file(de_path)
    
    print("\n" + "=" * 70)
    print("✅ CLEANUP COMPLETE")
    print("=" * 70)
    print(f"\n📊 Results:")
    print(f"   en.json: {en_removed} keys removed")
    print(f"   de.json: {de_removed} keys removed")
    
    if en_removed == de_removed == total_unused:
        print(f"\n✅ Successfully removed all {total_unused} unused keys!")
        return 0
    else:
        print(f"\n⚠️  Mismatch in removal counts. Please verify files manually.")
        return 1


if __name__ == "__main__":
    exit(main())
