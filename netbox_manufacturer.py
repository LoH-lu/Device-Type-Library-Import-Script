import configparser
import re
import sys
from pathlib import Path

import requests
# disable insecure warnings (netbox_connection uses verify=False)
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from netbox_connection import connect_to_netbox


def slugify(name: str) -> str:
    name = name.strip().lower()
    name = name.replace("&", "and")
    # replace non alnum with hyphens
    name = re.sub(r"[^a-z0-9]+", "-", name)
    name = name.strip("-")
    # collapse multiple hyphens
    name = re.sub(r"-{2,}", "-", name)
    return name


def find_input_dirs(start: Path) -> list[Path]:
    """
    Search upward for devicetype-library-master and return any of:
      - device-types
      - rack-types
      - module-types
    Returns list of Path objects (may contain one, two, or all three). Empty list if none found.
    """
    for p in (start, *start.parents):
        base = p / "devicetype-library-master"
        device = base / "device-types"
        rack = base / "rack-types"
        module = base / "module-types"
        found = []
        if device.exists():
            found.append(device)
        if rack.exists():
            found.append(rack)
        if module.exists():
            found.append(module)
        if found:
            return found
    return []


def read_credentials_ini(start: Path) -> tuple[str, str]:
    # search for var.ini in start and parents
    for p in (start, *start.parents):
        ini = p / "var.ini"
        if ini.exists():
            cfg = configparser.ConfigParser()
            cfg.read(ini)
            token = cfg.get("credentials", "token", fallback=None)
            url = cfg.get("credentials", "url", fallback=None)
            if token and url:
                return url.rstrip("/"), token
            break
    raise FileNotFoundError("Could not find var.ini with [credentials] token and url.")


def get_or_create_valid_tag(nb):
    """
    Get or create the 'Valid' tag in NetBox.
    Returns the tag object.
    """
    try:
        # Try to get existing 'Valid' tag
        tag = nb.extras.tags.get(slug="valid")
        if tag:
            return tag
    except Exception:
        pass
    
    try:
        # Create 'Valid' tag if it doesn't exist
        tag = nb.extras.tags.create({
            "name": "Valid",
            "slug": "valid",
            "color": "4caf50"  # Green color
        })
        print("INFO: Created 'Valid' tag")
        return tag
    except Exception as e:
        print(f"WARNING: Could not create 'Valid' tag: {e}")
        return None


def main():
    base = Path(__file__).resolve().parent
    input_dirs = find_input_dirs(base)
    if not input_dirs:
        print("ERROR: Could not find devicetype-library-master/device-types, devicetype-library-master/rack-types, or devicetype-library-master/module-types from script location.", file=sys.stderr)
        sys.exit(1)

    try:
        url, token = read_credentials_ini(base)
    except Exception as e:
        print(f"ERROR reading var.ini: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        nb = connect_to_netbox(url, token)
    except Exception as e:
        print(f"ERROR connecting to NetBox: {e}", file=sys.stderr)
        sys.exit(1)

    # Get or create the 'Valid' tag
    valid_tag = get_or_create_valid_tag(nb)

    created = 0
    skipped = 0
    updated = 0
    errors = 0

    for types_dir in input_dirs:
        print(f"INFO: Processing directory: {types_dir}")
        for entry in sorted(types_dir.iterdir()):
            if not entry.is_dir():
                continue
            # skip hidden or index dirs
            name = entry.name
            if name.startswith("."):
                continue

            slug = slugify(name)
            try:
                existing = nb.dcim.manufacturers.get(slug=slug)
            except Exception as e:
                print(f"WARNING: NetBox lookup failed for '{name}' (slug='{slug}'): {e}")
                existing = None

            if existing:
                # Check if the existing manufacturer already has the 'Valid' tag
                needs_tag = True
                if valid_tag and hasattr(existing, 'tags') and existing.tags:
                    existing_tag_ids = [tag.id for tag in existing.tags]
                    needs_tag = valid_tag.id not in existing_tag_ids
                
                if needs_tag and valid_tag:
                    try:
                        # Get current tags and add 'Valid' tag
                        current_tags = list(existing.tags) if hasattr(existing, 'tags') and existing.tags else []
                        current_tag_ids = [tag.id for tag in current_tags]
                        current_tag_ids.append(valid_tag.id)
                        
                        # Update the manufacturer with the new tag
                        existing.tags = current_tag_ids
                        existing.save()
                        updated += 1
                        print(f"UPDATE: '{name}' (slug '{slug}') - added 'Valid' tag")
                    except Exception as e:
                        print(f"WARNING: Failed to add 'Valid' tag to existing '{name}': {e}")
                        skipped += 1
                        print(f"SKIP: '{name}' exists as slug '{slug}'")
                else:
                    skipped += 1
                    tag_status = " (already has 'Valid' tag)" if valid_tag and not needs_tag else ""
                    print(f"SKIP: '{name}' exists as slug '{slug}'{tag_status}")
                continue

            try:
                # Prepare manufacturer data
                manufacturer_data = {"name": name, "slug": slug}
                
                # Add 'Valid' tag if it was created successfully
                if valid_tag:
                    manufacturer_data["tags"] = [valid_tag.id]
                
                nb.dcim.manufacturers.create(manufacturer_data)
                created += 1
                tag_info = " with 'Valid' tag" if valid_tag else ""
                print(f"CREATE: '{name}' -> slug '{slug}'{tag_info}")
            except Exception as e:
                errors += 1
                print(f"ERROR creating '{name}' (slug='{slug}'): {e}")

    print(f"Done. created={created}, updated={updated}, skipped={skipped}, errors={errors}")


if __name__ == "__main__":
    main()