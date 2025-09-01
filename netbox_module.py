import os
import glob
import yaml
import configparser
import urllib3
import re
from pathlib import Path
import traceback
from datetime import datetime

from netbox_connection import connect_to_netbox
from module_type_tracker import ModuleTypeTracker  # changed to ModuleTypeTracker

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ALLOWED_FIELDS = {
    "manufacturer", "model", "slug", "u_height", "part_number",
    "exclude_from_utilization", "is_full_depth", "subdevice_role", "airflow",
    "description", "weight", "weight_unit", "comments"
}

# mapping YAML component keys -> (netbox resource attribute, allowed fields for that component)
COMPONENT_MAP = {
    "console-ports": ("console_port_templates", {"name", "label", "type", "_is_power_source"}),
    "console-server-ports": ("console_server_port_templates", {"name", "label", "type"}),
    "power-ports": ("power_port_templates", {"name", "label", "type", "maximum_draw", "allocated_draw", "_is_power_source"}),
    "power-outlets": ("power_outlet_templates", {"name", "label", "type", "power_port", "feed_leg"}),
    "interfaces": ("interface_templates", {"name", "label", "type", "mgmt_only", "poe_mode", "poe_type"}),
    "rear-ports": ("rear_port_templates", {"name", "label", "type", "positions", "_is_power_source"}),
    "front-ports": ("front_port_templates", {"name", "label", "type", "rear_port", "rear_port_position"}),
    "module-bays": ("module_bay_templates", {"name", "label", "position"}),
    "device-bays": ("device_bay_templates", {"name", "label", "position"}),
    "inventory-items": ("inventory_item_templates", {"name", "label", "manufacturer", "part_id"}),
}

def slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s

def read_vars(varfile_path):
    cfg = configparser.ConfigParser()
    cfg.read(varfile_path)
    creds = cfg["credentials"]
    url = (creds.get("url") or "").rstrip("/")
    token = creds.get("token")
    return url, token

def ensure_valid_tag(netbox):
    """Ensure the 'Valid' tag exists in NetBox, create it if it doesn't"""
    try:
        tag = netbox.extras.tags.get(name="Valid")
        if tag:
            return tag.id
    except Exception:
        pass
    
    try:
        # Create the Valid tag if it doesn't exist
        tag_payload = {
            "name": "Valid",
            "slug": "valid",
            "description": "Indicates a validated module type",
            "color": "4caf50"  # Green color
        }
        created_tag = netbox.extras.tags.create(tag_payload)
        print(f"Created 'Valid' tag (id={created_tag.id})")
        return created_tag.id
    except Exception as e:
        print(f"WARNING: Failed to create 'Valid' tag: {e}")
        return None

def ensure_manufacturer(netbox, name):
    if not name:
        return None
    try:
        m = netbox.dcim.manufacturers.get(name=name)
        if m:
            return m.id
    except Exception:
        pass
    try:
        s = slugify(name)
        m = netbox.dcim.manufacturers.get(slug=s)
        if m:
            return m.id
    except Exception:
        pass
    print(f"WARNING: Manufacturer '{name}' not found in NetBox (tried name and slug='{slugify(name)}'). Skipping.")
    return None

def normalize_payload(raw):
    if not isinstance(raw, dict):
        return {}
    payload = {}
    for k, v in raw.items():
        if k not in ALLOWED_FIELDS:
            continue
        if k in ("exclude_from_utilization", "is_full_depth"):
            if isinstance(v, str):
                payload[k] = v.lower() in ("true", "yes", "1")
            else:
                payload[k] = bool(v)
        else:
            payload[k] = v
    return payload

def find_existing_module_type(netbox, payload):
    # try by slug
    if "slug" in payload and payload["slug"]:
        try:
            mt = netbox.dcim.module_types.get(slug=payload["slug"])
            if mt:
                return mt
        except Exception:
            pass
    # try by model + manufacturer if available
    if "model" in payload and "manufacturer" in payload and payload["manufacturer"]:
        try:
            candidates = netbox.dcim.module_types.filter(model=payload["model"])
            for c in candidates:
                try:
                    if hasattr(c, "manufacturer") and c.manufacturer and int(c.manufacturer.id) == int(payload["manufacturer"]):
                        return c
                except Exception:
                    continue
        except Exception:
            pass
    return None

def create_or_update_component(resource, module_type_obj, item, allowed_fields, nb=None, created_map=None):
    """
    resource: template resource manager (e.g., netbox.dcim.console_port_templates)
    module_type_obj: the module type Record
    item: YAML dict for the component
    """
    # build payload
    payload = {}
    for k, v in item.items():
        if k not in allowed_fields:
            continue

        if k == "position" and v is not None:
            # Force position to always be a string
            payload[k] = str(v)

        elif isinstance(v, str) and v.lower() in ("true", "false", "yes", "no"):
            # Boolean normalization only for real booleans, not "0"/"1" positions
            payload[k] = v.lower() in ("true", "yes", "1")

        else:
            payload[k] = v

    # Always include module_type for template creation
    payload["module_type"] = module_type_obj.id

    # NetBox API instance for lookups: prefer explicitly passed nb, fallback to resource internals
    nb_api = nb
    if nb_api is None:
        try:
            nb_api = resource._parent._api
        except Exception:
            nb_api = None

    # Resolve references that expect IDs using template API queries
    # front-ports.rear_port -> prefer IDs created earlier in this run, then lookup by name in dcim.rear_port_templates filtered by module_type
    if "rear_port" in payload and payload["rear_port"] is not None:
        try:
            # 1) check created_map for an earlier-created rear port in this run
            name_val = payload["rear_port"]
            if created_map:
                # created_map keys use the NetBox resource name (e.g. "rear_port_templates")
                cmap = created_map.get("rear_port_templates", {})
                if name_val in cmap:
                    payload["rear_port"] = cmap[name_val]
                else:
                    # also try string variants
                    val_str = str(name_val)
                    if val_str in cmap:
                        payload["rear_port"] = cmap[val_str]
            # 2) if not resolved yet, try NetBox API filtered by module_type
            if not isinstance(payload["rear_port"], int):
                if nb_api:
                    rear_candidates = list(nb_api.dcim.rear_port_templates.filter(
                        module_type=module_type_obj.id,
                        name=name_val
                    ))
                    if not rear_candidates:
                        # fallback: fetch all rear ports for module_type and match locally
                        all_rear_ports = list(nb_api.dcim.rear_port_templates.filter(module_type=module_type_obj.id))
                        rear_candidates = [r for r in all_rear_ports if r.name == name_val]
                    rear = rear_candidates[0] if rear_candidates else None
                    if rear:
                        payload["rear_port"] = rear.id
                    else:
                        print(f"    WARNING: rear_port '{name_val}' not found for module_type {module_type_obj.id}")
                        return ("error", f"rear_port '{name_val}' not found")
                else:
                    return ("error", "NetBox API not available for rear_port lookup")
        except Exception as e:
            return ("error", f"rear_port lookup failed: {e}")

    # power-outlets.power_port -> lookup in dcim.power_port_templates filtered by module_type
    if "power_port" in payload and payload["power_port"] is not None:
        try:
            # prefer IDs created earlier in this run (created_map), then fall back to NetBox API
            name_val = payload["power_port"]
            if created_map:
                cmap = created_map.get("power_port_templates", {})
                if name_val in cmap:
                    payload["power_port"] = cmap[name_val]
                else:
                    val_str = str(name_val)
                    if val_str in cmap:
                        payload["power_port"] = cmap[val_str]

            if not isinstance(payload.get("power_port"), int):
                if nb_api:
                    pwr_candidates = list(nb_api.dcim.power_port_templates.filter(
                        module_type=module_type_obj.id,
                        name=name_val
                    ))
                    pwr = next(iter(pwr_candidates), None)
                    if pwr:
                        payload["power_port"] = pwr.id
                    else:
                        # not found for this module type -> remove to avoid creating invalid reference
                        payload.pop("power_port", None)
                else:
                    payload.pop("power_port", None)
        except Exception:
            # best-effort: if lookup fails, just drop the reference so creation can proceed
            payload.pop("power_port", None)

    # inventory-items.manufacturer -> convert manufacturer name to id if provided
    if "manufacturer" in payload and payload["manufacturer"]:
        try:
            man_id = ensure_manufacturer(nb_api, payload["manufacturer"]) if nb_api else None
            if man_id:
                payload["manufacturer"] = man_id
            else:
                payload.pop("manufacturer", None)
        except Exception:
            payload.pop("manufacturer", None)

    # Find existing template by name + module_type
    existing = None
    try:
        if "name" in payload and payload["name"] is not None:
            # Query NetBox for templates by name (could return from other module types too)
            candidates = list(resource.filter(name=payload["name"]))
            
            # Keep only ones that belong to THIS module type
            candidates = [
                c for c in candidates
                if getattr(c, "module_type", None) and getattr(c.module_type, "id", None) == module_type_obj.id
            ]

            if candidates:
                existing = candidates[0]
    except Exception as e:
        print(f"    DEBUG: Error finding existing template '{payload.get('name')}': {e}")
        existing = None

    if existing:
        try:
            if hasattr(existing, 'module_type') and existing.module_type.id == module_type_obj.id:
                # Build payload without module_type for comparison
                update_payload = {k: v for k, v in payload.items() if k != "module_type"}

                # Check if any field actually differs
                needs_update = any(
                    getattr(existing, k, None) != v for k, v in update_payload.items()
                )

                if needs_update:
                    existing.update(update_payload)
                    return ("updated", existing.id)
                else:
                    # Already exists and no update needed → just skip
                    return ("skipped", existing.id)
            else:
                print("DEBUG: Existing template belongs to different module type, creating new one")
                existing = None
        except Exception as e:
            print(f"DEBUG: Failed to check/update existing template: {e}")
            existing = None

    if not existing:
        try:
            created = resource.create(payload)
            return ("created", created.id)
        except Exception as e:
            return ("error", str(e))

def write_error_log(filepath, relative_path, message, exc=None, base_dir=None):
    """Write error details to a per-module-type log file under module_type_logs/"""
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir = os.path.join(base_dir, "module_type_logs")  # changed from device_type_logs
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except Exception:
        pass
    safe_name = re.sub(r'[\\/:\s]+', '_', relative_path or os.path.basename(filepath))
    safe_name = re.sub(r'[^0-9A-Za-z._-]', '_', safe_name)
    log_path = os.path.join(logs_dir, f"{safe_name}.log")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat()}] {message}\n")
            if exc is not None:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=fh)
            fh.write("\n")
    except Exception:
        # best-effort logging; don't raise further
        pass

def process_components(netbox, module_type_obj, doc, filepath=None, base_dir=None, relative_path=None):
    mt_id = module_type_obj.id
    # map of created templates during this processing run:
    # { "rear_port_templates": { "1": 3558, ... }, "power_port_templates": {...}, ... }
    created_map = {}

    # create rear-port-templates first (front-port-templates may reference them)
    ordered_keys = list(COMPONENT_MAP.keys())
    if "rear-ports" in ordered_keys:
        ordered_keys.remove("rear-ports")
        ordered_keys.insert(0, "rear-ports")

    for yaml_key in ordered_keys:
        resource_name, allowed = COMPONENT_MAP[yaml_key]
        items = doc.get(yaml_key)
        if not items:
            continue

        # Use template resource accessor
        resource = getattr(netbox.dcim, resource_name, None)
        if resource is None:
            msg = f"NetBox resource for '{resource_name}' not available. Skipping {yaml_key}."
            print(f"WARNING: {msg}")
            # log the missing resource as a warning for this module-type
            write_error_log(filepath, relative_path, msg, base_dir=base_dir)
            continue

        for item in items if isinstance(items, list) else [items]:
            status, info = create_or_update_component(resource, module_type_obj, item, allowed, nb=netbox, created_map=created_map)
            if status == "created":
                print(f"  Created {resource_name} '{item.get('name')}' (id={info})")
                # record created template so subsequent lookups in the same file can resolve references
                try:
                    nm = item.get("name")
                    if nm:
                        created_map.setdefault(resource_name, {})[str(nm)] = info
                except Exception:
                    pass
            elif status == "updated":
                print(f"  Updated {resource_name} '{item.get('name')}' (id={info})")
                # record updated template id (overwrite if present)
                try:
                    nm = item.get("name")
                    if nm:
                        created_map.setdefault(resource_name, {})[str(nm)] = info
                except Exception:
                    pass
            elif status == "skipped":
                print(f"  Skipped {resource_name} '{item.get('name')}' (already exists, no changes)")
            else:
                # status == "error"
                err_msg = f"Failed {resource_name} '{item.get('name')}': {info}"
                print(f"  {err_msg}")
                write_error_log(filepath, relative_path, err_msg, base_dir=base_dir)

def process_file(netbox, filepath, tracker=None):
    """Enhanced process_file function with tracking support"""
    # Get the Valid tag ID
    valid_tag_id = ensure_valid_tag(netbox)
    
    # determine base_dir for logging
    script_base_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = tracker.base_dir if tracker else script_base_dir

    # Get relative path for tracking / log filename
    relative_path = None
    module_types_dir = os.path.join(base_dir, "devicetype-library-master", "module-types")
    try:
        if os.path.commonpath([os.path.abspath(filepath), os.path.abspath(module_types_dir)]) == os.path.abspath(module_types_dir):
            relative_path = os.path.relpath(filepath, module_types_dir)
    except Exception:
        # fallback: no relative path
        relative_path = os.path.basename(filepath)

    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        if raw is None:
            msg = f"Empty YAML file: {filepath}"
            print(f"Skipping empty file: {filepath}")
            write_error_log(filepath, relative_path, msg, base_dir=base_dir)
            if tracker and relative_path:
                tracker.mark_as_skipped(relative_path, "Empty YAML file")
            return
            
        docs = raw if isinstance(raw, list) else [raw]
        
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            payload = normalize_payload(doc)

            # manufacturer: prefer YAML manufacturer, otherwise infer from parent folder
            man_name = doc.get("manufacturer") or Path(filepath).parent.name
            man_id = ensure_manufacturer(netbox, man_name)
            if man_id:
                payload["manufacturer"] = man_id
            else:
                error_msg = f"Manufacturer '{man_name}' not present in NetBox"
                print(f"Skipping {filepath}: {error_msg}")
                write_error_log(filepath, relative_path, error_msg, base_dir=base_dir)
                if tracker and relative_path:
                    tracker.mark_as_failed(relative_path, error_msg)
                continue

            existing = find_existing_module_type(netbox, payload)
            module_type_obj = None
            netbox_id = None
            
            if existing:
                try:
                    existing.update(payload)
                    module_type_obj = existing
                    netbox_id = existing.id
                    print(f"Updated ModuleType: {payload.get('model') or payload.get('slug')} (id={existing.id})")
                except Exception as e:
                    error_msg = f"Failed to update: {e}"
                    print(f"Failed to update {filepath}: {e}")
                    write_error_log(filepath, relative_path, error_msg, exc=e, base_dir=base_dir)
                    if tracker and relative_path:
                        tracker.mark_as_failed(relative_path, error_msg)
                    continue
            else:
                try:
                    created = netbox.dcim.module_types.create(payload)
                    module_type_obj = created
                    netbox_id = created.id
                    
                    # Add the 'Valid' tag to newly created module types
                    if valid_tag_id:
                        try:
                            # Get current tags and add Valid tag
                            current_tags = getattr(created, 'tags', [])
                            tag_ids = [tag.id for tag in current_tags] if current_tags else []
                            if valid_tag_id not in tag_ids:
                                tag_ids.append(valid_tag_id)
                                created.update({"tags": tag_ids})
                                print(f"  Added 'Valid' tag to module type")
                        except Exception as e:
                            print(f"  WARNING: Failed to add 'Valid' tag: {e}")
                    
                    print(f"Created ModuleType: {payload.get('model') or payload.get('slug')} (id={created.id})")
                except Exception as e:
                    error_msg = f"Failed to create: {e}"
                    print(f"Failed to create from {filepath}: {e}")
                    write_error_log(filepath, relative_path, error_msg, exc=e, base_dir=base_dir)
                    if tracker and relative_path:
                        tracker.mark_as_failed(relative_path, error_msg)
                    continue

            # create/update component templates (interfaces, ports, etc.)
            try:
                process_components(netbox, module_type_obj, doc, filepath=filepath, base_dir=base_dir, relative_path=relative_path)
                
                # Mark as successfully created in tracker
                if tracker and relative_path:
                    tracker.mark_as_created(relative_path, netbox_id)
                    
            except Exception as e:
                error_msg = f"Failed to process components: {e}"
                print(f"Error processing components for {filepath}: {e}")
                write_error_log(filepath, relative_path, error_msg, exc=e, base_dir=base_dir)
                if tracker and relative_path:
                    tracker.mark_as_failed(relative_path, error_msg)
                    
    except Exception as e:
        error_msg = f"File processing error: {e}"
        print(f"Error processing file {filepath}: {e}")
        write_error_log(filepath, relative_path, error_msg, exc=e, base_dir=base_dir)
        if tracker and relative_path:
            tracker.mark_as_failed(relative_path, error_msg)

def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    varfile = os.path.join(base_dir, "var.ini")
    if not os.path.exists(varfile):
        print("var.ini not found in script directory.")
        return
        
    url, token = read_vars(varfile)
    netbox = connect_to_netbox(url, token)
    
    # Initialize the tracker
    tracker = ModuleTypeTracker(base_dir)
    print("Scanning module-types directory and initializing tracker...")
    if not tracker.scan_module_types():
        print("Failed to initialize tracker. Proceeding without tracking.")
        tracker = None
    else:
        print(f"Tracker initialized. Found {tracker.progress_data['total_files']} module type files.")
        tracker.print_summary()
    
    # Ask user if they want to process all files or just pending ones
    if tracker:
        pending_count = len(tracker.get_pending_module_types())
        if pending_count < tracker.progress_data['total_files']:
            print(f"\nFound {pending_count} pending module types out of {tracker.progress_data['total_files']} total.")
            choice = input("Process only pending files? (y/n): ").lower().strip()
            if choice == 'y':
                print("Processing only pending module types...")
                pending = tracker.get_pending_module_types()
                for relative_path, info in pending:
                    filepath = info['full_path']
                    print(f"Processing: {filepath}")
                    process_file(netbox, filepath, tracker)
                
                print("\nProcessing completed!")
                tracker.print_summary()
                return
    
    # Process all files (module-types)
    module_types_dir = os.path.join(base_dir, "devicetype-library-master", "module-types")
    if not os.path.isdir(module_types_dir):
        print("module-types directory not found:", module_types_dir)
        return
        
    patterns = ("**/*.yml", "**/*.yaml")
    files = []
    for p in patterns:
        files.extend(glob.glob(os.path.join(module_types_dir, p), recursive=True))
    if not files:
        print("No YAML files found under:", module_types_dir)
        return
        
    for f in files:
        print(f"Processing: {f}")
        process_file(netbox, f, tracker)
    
    # Print final summary if tracker is available
    if tracker:
        print("\nFinal Summary:")
        tracker.print_summary()

if __name__ == "__main__":
    main()
