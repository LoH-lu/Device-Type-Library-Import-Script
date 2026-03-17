"""
netbox_images.py - Upload elevation images from the devicetype-library to NetBox.

Scans the elevation-images/ directory inside the devicetype-library-master folder,
matches images to existing device types in NetBox by manufacturer + slug, and
uploads front/rear images via the NetBox REST API (multipart PATCH).

Designed to integrate with the LoH-lu Device-Type-Library-Import-Script.
"""

import os
import json
import glob
import requests
import urllib3

# Suppress InsecureRequestWarning if SSL verify is disabled
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration ──────────────────────────────────────────────────────────────

PROGRESS_FILE = "image_upload_progress.json"

# Supported image extensions (what NetBox accepts)
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".svgz",
    ".tif", ".tiff", ".ico", ".avif", ".apng", ".pjp", ".jfif", ".pjpeg", ".xbm"
}

# MIME type mapping for common formats
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".svgz": "image/svg+xml",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".apng": "image/apng",
}


# ─── Progress Tracking ──────────────────────────────────────────────────────────

def load_progress():
    """Load progress from the JSON tracking file."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "summary": {
            "total_images_found": 0,
            "uploaded": 0,
            "skipped_already_exists": 0,
            "skipped_no_match": 0,
            "failed": 0
        },
        "completed": {}  # key: "manufacturer/slug.face" -> status
    }


def save_progress(progress):
    """Save progress to the JSON tracking file."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)


# ─── Image Discovery ────────────────────────────────────────────────────────────

def discover_elevation_images(library_path):
    """
    Scan the elevation-images/ directory structure.
    
    Structure: elevation-images/<Manufacturer>/<slug>.front.<ext>
               elevation-images/<Manufacturer>/<slug>.rear.<ext>
    
    Returns a dict:
        {
            "Manufacturer": {
                "slug-name": {
                    "front": "/full/path/to/slug-name.front.png",
                    "rear": "/full/path/to/slug-name.rear.png"
                }
            }
        }
    """
    elevation_dir = os.path.join(library_path, "elevation-images")
    
    if not os.path.isdir(elevation_dir):
        print(f"[Images] WARNING: elevation-images directory not found at: {elevation_dir}")
        return {}
    
    images = {}
    
    for manufacturer_name in sorted(os.listdir(elevation_dir)):
        manufacturer_path = os.path.join(elevation_dir, manufacturer_name)
        if not os.path.isdir(manufacturer_path):
            continue
        
        for filename in sorted(os.listdir(manufacturer_path)):
            filepath = os.path.join(manufacturer_path, filename)
            if not os.path.isfile(filepath):
                continue
            
            # Parse filename: <slug>.<face>.<ext>
            # e.g., "juniper-mx204.front.png" or "cisco-c9300-24t.rear.jpg"
            parts = filename.rsplit(".", 2)
            if len(parts) != 3:
                continue
            
            slug, face, ext = parts
            ext_with_dot = f".{ext.lower()}"
            face_lower = face.lower()
            
            if ext_with_dot not in IMAGE_EXTENSIONS:
                continue
            if face_lower not in ("front", "rear"):
                continue
            
            if manufacturer_name not in images:
                images[manufacturer_name] = {}
            if slug not in images[manufacturer_name]:
                images[manufacturer_name][slug] = {}
            
            images[manufacturer_name][slug][face_lower] = filepath
    
    # Count totals
    total = sum(
        len(faces)
        for mfr in images.values()
        for faces in mfr.values()
    )
    print(f"[Images] Discovered {total} elevation images across {len(images)} manufacturers")
    
    return images


# ─── NetBox API Helpers ──────────────────────────────────────────────────────────

def get_all_device_types(nb_url, nb_token, verify_ssl=True):
    """
    Fetch all device types from NetBox, paginating through all results.
    
    Returns a dict keyed by (manufacturer_slug, device_type_slug) -> device_type_id
    Also returns a lookup: (manufacturer_name_lower, device_type_slug) -> device_type_id
    """
    headers = {
        "Authorization": f"Token {nb_token}",
        "Accept": "application/json"
    }
    
    device_types = {}
    device_types_by_name = {}
    url = f"{nb_url.rstrip('/')}/api/dcim/device-types/?limit=1000&offset=0"
    
    while url:
        response = requests.get(url, headers=headers, verify=verify_ssl, timeout=30)
        response.raise_for_status()
        data = response.json()
        
        for dt in data.get("results", []):
            dt_id = dt["id"]
            dt_slug = dt["slug"]
            mfr_slug = dt.get("manufacturer", {}).get("slug", "")
            mfr_name = dt.get("manufacturer", {}).get("name", "")
            
            # Check if images already exist
            has_front = bool(dt.get("front_image"))
            has_rear = bool(dt.get("rear_image"))
            
            device_types[(mfr_slug, dt_slug)] = {
                "id": dt_id,
                "has_front": has_front,
                "has_rear": has_rear,
                "manufacturer_name": mfr_name,
                "manufacturer_slug": mfr_slug
            }
            # Also index by manufacturer display name (lowercase) for fuzzy matching
            device_types_by_name[(mfr_name.lower(), dt_slug)] = device_types[(mfr_slug, dt_slug)]
        
        url = data.get("next")
    
    print(f"[Images] Fetched {len(device_types)} device types from NetBox")
    return device_types, device_types_by_name


def upload_image(nb_url, nb_token, device_type_id, face, image_path, verify_ssl=True):
    """
    Upload a front or rear image to a device type via multipart PATCH.
    
    face: "front" or "rear"
    Returns: (success: bool, status_code: int, response_text: str)
    """
    url = f"{nb_url.rstrip('/')}/api/dcim/device-types/{device_type_id}/"
    headers = {
        "Authorization": f"Token {nb_token}"
        # Do NOT set Content-Type — requests sets it with the multipart boundary
    }
    
    filename = os.path.basename(image_path)
    ext = os.path.splitext(filename)[1].lower()
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    
    field_name = f"{face}_image"
    
    with open(image_path, "rb") as img_file:
        files = {
            field_name: (filename, img_file, mime)
        }
        response = requests.patch(
            url,
            headers=headers,
            files=files,
            verify=verify_ssl,
            timeout=60
        )
    
    success = response.status_code in (200, 201)
    return success, response.status_code, response.text


# ─── Manufacturer Name Matching ─────────────────────────────────────────────────

def normalize_manufacturer_name(name):
    """Normalize manufacturer name for matching between library folder names and NetBox."""
    return name.lower().strip().replace(" ", "-").replace("_", "-")


def find_device_type_match(manufacturer_folder, slug, device_types, device_types_by_name):
    """
    Try to match a library image to a NetBox device type.
    
    The elevation-images folder uses the manufacturer's display name (e.g., "Juniper"),
    while NetBox might store it as a slug (e.g., "juniper").
    
    Returns the device type info dict or None.
    """
    # Try direct match by manufacturer folder name (lowercase) + slug
    key = (manufacturer_folder.lower(), slug)
    if key in device_types_by_name:
        return device_types_by_name[key]
    
    # Try normalized manufacturer name as slug
    mfr_slug = normalize_manufacturer_name(manufacturer_folder)
    key = (mfr_slug, slug)
    if key in device_types:
        return device_types[key]
    
    # Try some common variations
    # e.g., "Hewlett Packard Enterprise" -> "hpe", "HP" -> "hp"
    for (mfr_s, dt_s), dt_info in device_types.items():
        if dt_s == slug:
            # Check if manufacturer names are related
            mfr_name_lower = dt_info["manufacturer_name"].lower()
            folder_lower = manufacturer_folder.lower()
            if (folder_lower in mfr_name_lower or
                mfr_name_lower in folder_lower or
                mfr_s == normalize_manufacturer_name(manufacturer_folder)):
                return dt_info
    
    return None


# ─── Main Upload Logic ──────────────────────────────────────────────────────────

def upload_elevation_images(nb_url, nb_token, library_path, verify_ssl=True, overwrite=False):
    """
    Main function: discover images, match to device types, upload to NetBox.
    
    Parameters:
        nb_url: NetBox base URL (e.g., "https://netbox.example.com")
        nb_token: NetBox API token
        library_path: Path to the devicetype-library-master folder
        verify_ssl: Whether to verify SSL certificates
        overwrite: If True, re-upload even if image already exists in NetBox
    
    Returns:
        progress dict with summary stats
    """
    print("\n" + "=" * 70)
    print("  ELEVATION IMAGE UPLOAD")
    print("=" * 70)
    
    # Load existing progress
    progress = load_progress()
    
    # Step 1: Discover images in the library
    images = discover_elevation_images(library_path)
    if not images:
        print("[Images] No elevation images found. Skipping.")
        return progress
    
    # Step 2: Fetch all device types from NetBox
    print("[Images] Fetching device types from NetBox...")
    device_types, device_types_by_name = get_all_device_types(nb_url, nb_token, verify_ssl)
    
    if not device_types:
        print("[Images] No device types found in NetBox. Run device type import first.")
        return progress
    
    # Step 3: Match and upload
    total_found = 0
    uploaded = 0
    skipped_exists = 0
    skipped_no_match = 0
    failed = 0
    
    for manufacturer_folder, slugs in sorted(images.items()):
        print(f"\n[Images] Processing manufacturer: {manufacturer_folder}")
        
        for slug, faces in sorted(slugs.items()):
            dt_info = find_device_type_match(
                manufacturer_folder, slug, device_types, device_types_by_name
            )
            
            if dt_info is None:
                for face in faces:
                    total_found += 1
                    skipped_no_match += 1
                    progress_key = f"{manufacturer_folder}/{slug}.{face}"
                    if progress_key not in progress["completed"]:
                        progress["completed"][progress_key] = "skipped_no_match"
                continue
            
            dt_id = dt_info["id"]
            
            for face, image_path in sorted(faces.items()):
                total_found += 1
                progress_key = f"{manufacturer_folder}/{slug}.{face}"
                
                # Check if already completed in a previous run
                if progress_key in progress["completed"] and \
                   progress["completed"][progress_key] == "uploaded":
                    skipped_exists += 1
                    continue
                
                # Check if image already exists in NetBox (unless overwrite mode)
                if not overwrite:
                    existing_key = f"has_{face}"
                    if dt_info.get(existing_key, False):
                        print(f"  [SKIP] {slug} {face} image already exists in NetBox")
                        skipped_exists += 1
                        progress["completed"][progress_key] = "skipped_already_exists"
                        save_progress(progress)
                        continue
                
                # Upload
                print(f"  [UPLOAD] {slug} -> {face} image (ID: {dt_id})...", end=" ")
                try:
                    success, status_code, resp_text = upload_image(
                        nb_url, nb_token, dt_id, face, image_path, verify_ssl
                    )
                    
                    if success:
                        print(f"OK ({status_code})")
                        uploaded += 1
                        progress["completed"][progress_key] = "uploaded"
                        # Update dt_info so we don't try to re-upload rear if front just succeeded
                        dt_info[f"has_{face}"] = True
                    else:
                        print(f"FAILED ({status_code})")
                        # Try to extract error message
                        try:
                            err = json.loads(resp_text)
                            print(f"         Error: {err}")
                        except json.JSONDecodeError:
                            print(f"         Response: {resp_text[:200]}")
                        failed += 1
                        progress["completed"][progress_key] = f"failed_{status_code}"
                        
                except requests.exceptions.RequestException as e:
                    print(f"ERROR: {e}")
                    failed += 1
                    progress["completed"][progress_key] = f"error_{type(e).__name__}"
                
                # Save progress after each image
                save_progress(progress)
    
    # Update summary
    progress["summary"] = {
        "total_images_found": total_found,
        "uploaded": uploaded,
        "skipped_already_exists": skipped_exists,
        "skipped_no_match": skipped_no_match,
        "failed": failed
    }
    save_progress(progress)
    
    # Print summary
    print("\n" + "-" * 70)
    print(f"  Image Upload Summary:")
    print(f"    Total images found:      {total_found}")
    print(f"    Successfully uploaded:    {uploaded}")
    print(f"    Skipped (already exists): {skipped_exists}")
    print(f"    Skipped (no match):       {skipped_no_match}")
    print(f"    Failed:                   {failed}")
    print("-" * 70 + "\n")
    
    return progress


# ─── Standalone Execution ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import configparser
    
    # Read config from var.ini (same as the rest of the project)
    config = configparser.ConfigParser()
    config.read("var.ini")
    
    try:
        nb_url = config.get("credentials", "url")
        nb_token = config.get("credentials", "token")
    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        print(f"[Images] ERROR: Could not read NetBox config from var.ini: {e}")
        print("         Make sure var.ini has [credentials] section with 'url' and 'token' keys.")
        exit(1)
    
    # Optional: SSL verification setting
    verify_ssl = config.getboolean("credentials", "verify_ssl", fallback=True)
    
    # Optional: overwrite existing images
    overwrite = config.getboolean("images", "overwrite", fallback=False)
    
    # Library path - default to devicetype-library-master in current directory
    library_path = config.get(
        "images", "library_path",
        fallback="devicetype-library-master"
    )
    
    if not os.path.isdir(library_path):
        print(f"[Images] ERROR: Library path not found: {library_path}")
        print("         Download the devicetype-library ZIP and extract it here.")
        exit(1)
    
    upload_elevation_images(
        nb_url=nb_url,
        nb_token=nb_token,
        library_path=library_path,
        verify_ssl=verify_ssl,
        overwrite=overwrite
    )
