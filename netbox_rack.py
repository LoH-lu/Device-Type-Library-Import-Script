import os
import glob
import yaml
import configparser
import urllib3
from pathlib import Path

from netbox_connection import connect_to_netbox

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ALLOWED_FIELDS = {
    "manufacturer", "model", "slug", "width", "u_height", "form_factor",
    "starting_unit", "desc_units", "outer_width", "outer_height",
    "outer_depth", "outer_unit", "mounting_depth", "weight", "max_weight",
    "weight_unit", "description", "comments", "tags", "id"
}

def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("&", "and")
    import re
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s

def read_vars(varfile_path):
    cfg = configparser.ConfigParser()
    cfg.read(varfile_path)
    creds = cfg["credentials"]
    url = creds.get("url").rstrip("/")
    token = creds.get("token")
    return url, token

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

def ensure_manufacturer(netbox, name):
    """
    Use existing manufacturer only. Try lookup by name, then by slug.
    Return manufacturer id or None if not found.
    """
    if not name:
        return None
    # try by exact name
    try:
        m = netbox.dcim.manufacturers.get(name=name)
        if m:
            return m.id
    except Exception:
        pass
    # try by slug
    try:
        s = slugify(name)
        m = netbox.dcim.manufacturers.get(slug=s)
        if m:
            return m.id
    except Exception:
        pass
    print(f"WARNING: Manufacturer '{name}' not found in NetBox (tried name and slug='{slugify(name)}'). Skipping.")
    return None

def ensure_tags(netbox, tags_value, valid_tag=None):
    if not tags_value:
        tags_list = []
    else:
        # tags may be a list or a comma-separated string of slugs
        if isinstance(tags_value, str):
            tags_list = [t.strip() for t in tags_value.split(",") if t.strip()]
        elif isinstance(tags_value, list):
            tags_list = [str(t).strip() for t in tags_value if str(t).strip()]
        else:
            tags_list = []
    
    # Always add 'valid' tag if available
    if valid_tag and valid_tag.slug not in tags_list:
        tags_list.append(valid_tag.slug)
    
    tag_ids = []
    for slug in tags_list:
        t = netbox.extras.tags.get(slug=slug)
        if not t:
            # create tag; use slug as name if no nicer name available
            t = netbox.extras.tags.create({"name": slug, "slug": slug})
        tag_ids.append(t.id)
    return tag_ids

def normalize_payload(raw):
    if not isinstance(raw, dict):
        return {}
    payload = {}
    for k, v in raw.items():
        if k not in ALLOWED_FIELDS:
            continue
        # normalize boolean-like fields
        if k == "desc_units":
            if isinstance(v, str):
                payload[k] = v.lower() in ("true", "yes", "1")
            else:
                payload[k] = bool(v)
        else:
            payload[k] = v
    return payload

def find_existing_rack_type(netbox, payload):
    # try by id
    if "id" in payload and payload["id"]:
        try:
            rt = netbox.dcim.rack_types.get(id=int(payload["id"]))
            if rt:
                return rt
        except Exception:
            pass
    # try by slug
    if "slug" in payload and payload["slug"]:
        rt = netbox.dcim.rack_types.get(slug=payload["slug"])
        if rt:
            return rt
    # try by model + manufacturer if available
    if "model" in payload and "manufacturer" in payload and payload["manufacturer"]:
        candidates = netbox.dcim.rack_types.filter(model=payload["model"])
        for c in candidates:
            try:
                if hasattr(c, "manufacturer") and c.manufacturer and int(c.manufacturer.id) == int(payload["manufacturer"]):
                    return c
            except Exception:
                continue
    return None

def has_valid_tag(existing_rack_type, valid_tag):
    """Check if the rack type already has the 'Valid' tag"""
    if not valid_tag or not hasattr(existing_rack_type, 'tags') or not existing_rack_type.tags:
        return False
    
    existing_tag_ids = []
    for tag in existing_rack_type.tags:
        if hasattr(tag, 'id'):
            # Tag is an object
            existing_tag_ids.append(tag.id)
        else:
            # Tag is already an ID
            existing_tag_ids.append(int(tag))
    
    return valid_tag.id in existing_tag_ids

def add_valid_tag_to_existing(netbox, existing_rack_type, valid_tag):
    """Add 'Valid' tag to existing rack type that doesn't have it"""
    if not valid_tag:
        return False
    
    try:
        # Get current tags - they may be IDs or objects depending on NetBox version
        current_tags = list(existing_rack_type.tags) if hasattr(existing_rack_type, 'tags') and existing_rack_type.tags else []
        current_tag_ids = []
        
        for tag in current_tags:
            if hasattr(tag, 'id'):
                # Tag is an object
                current_tag_ids.append(tag.id)
            else:
                # Tag is already an ID
                current_tag_ids.append(int(tag))
        
        # Add the valid tag ID if not already present
        if valid_tag.id not in current_tag_ids:
            current_tag_ids.append(valid_tag.id)
            
            # Update the rack type with the new tag
            existing_rack_type.tags = current_tag_ids
            existing_rack_type.save()
            return True
        return False  # Tag was already present
    except Exception as e:
        print(f"WARNING: Failed to add 'Valid' tag to existing rack type: {e}")
        return False

def process_file(netbox, filepath, valid_tag=None, stats=None):
    if stats is None:
        stats = {"created": 0, "updated": 0, "tagged": 0, "skipped": 0, "errors": 0}
    
    with open(filepath, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        print(f"Skipping empty file: {filepath}")
        return stats
    
    # if YAML contains a top-level list or multiple documents, handle first dict or each dict
    docs = raw if isinstance(raw, list) else [raw]
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        payload = normalize_payload(doc)

        # manufacturer handling: prefer YAML manufacturer, otherwise infer from parent folder
        man_name = None
        if "manufacturer" in doc and doc["manufacturer"]:
            man_name = doc["manufacturer"]
        else:
            # infer from parent folder (rack-types/<Manufacturer>/<file>.yml)
            parent = Path(filepath).parent
            man_name = parent.name

        man_id = ensure_manufacturer(netbox, man_name)
        if man_id:
            payload["manufacturer"] = man_id
        else:
            # manufacturer not found -> skip this rack type (per note manufacturers are pre-created)
            print(f"Skipping {filepath}: manufacturer '{man_name}' not present in NetBox.")
            stats["skipped"] += 1
            continue

        # tags handling - always include 'Valid' tag
        payload["tags"] = ensure_tags(netbox, doc.get("tags"), valid_tag)
        
        existing = find_existing_rack_type(netbox, payload)
        if existing:
            # Check if we need to add the 'Valid' tag to existing rack type
            needs_valid_tag = valid_tag and not has_valid_tag(existing, valid_tag)
            
            try:
                existing.update(payload)
                model_name = payload.get('model') or payload.get('slug')
                print(f"Updated RackType: {model_name} (id={existing.id})")
                stats["updated"] += 1
                
                # If the update didn't include the Valid tag, add it separately
                if needs_valid_tag:
                    if add_valid_tag_to_existing(netbox, existing, valid_tag):
                        print(f"  -> Added 'Valid' tag to {model_name}")
                        stats["tagged"] += 1
                        
            except Exception as e:
                print(f"Failed to update {filepath}: {e}")
                stats["errors"] += 1
        else:
            try:
                created = netbox.dcim.rack_types.create(payload)
                model_name = payload.get('model') or payload.get('slug')
                tag_info = " with 'Valid' tag" if valid_tag else ""
                print(f"Created RackType: {model_name} (id={created.id}){tag_info}")
                stats["created"] += 1
            except Exception as e:
                print(f"Failed to create from {filepath}: {e}")
                stats["errors"] += 1
    
    return stats

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    varfile = os.path.join(base_dir, "var.ini")
    if not os.path.exists(varfile):
        print("var.ini not found in script directory.")
        return
    url, token = read_vars(varfile)
    netbox = connect_to_netbox(url, token)
    
    # Get or create the 'Valid' tag
    valid_tag = get_or_create_valid_tag(netbox)
    
    # path to rack-types folder (relative to this repository)
    rack_types_dir = os.path.join(base_dir, "devicetype-library-master", "rack-types")
    if not os.path.isdir(rack_types_dir):
        print("rack-types directory not found:", rack_types_dir)
        return
    # find all .yml and .yaml
    patterns = ("**/*.yml", "**/*.yaml")
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(rack_types_dir, p), recursive=True))
    if not files:
        print("No YAML files found under:", rack_types_dir)
        return
    
    stats = {"created": 0, "updated": 0, "tagged": 0, "skipped": 0, "errors": 0}
    for f in files:
        stats = process_file(netbox, f, valid_tag, stats)
    
    print(f"Done. created={stats['created']}, updated={stats['updated']}, tagged={stats['tagged']}, skipped={stats['skipped']}, errors={stats['errors']}")

if __name__ == "__main__":
    main()