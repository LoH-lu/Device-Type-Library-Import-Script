import os
import glob
import yaml
import json
from pathlib import Path
from datetime import datetime

class ModuleTypeTracker:
    def __init__(self, base_dir=None):
        """
        Initialize the module type tracker

        Args:
            base_dir: Base directory where the script is located. If None, uses this file's directory.
        """
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        # use module-types directory
        self.module_types_dir = os.path.join(self.base_dir, "devicetype-library-master", "module-types")
        # track progress in a file named for module types
        self.tracking_file = os.path.join(self.base_dir, "module_type_progress.json")
        self.progress_data = self.load_progress()

    def load_progress(self):
        """Load existing progress data or create new tracking structure"""
        if os.path.exists(self.tracking_file):
            try:
                with open(self.tracking_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass

        return {
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
            "total_files": 0,
            "processed_count": 0,
            "failed_count": 0,
            "module_types": {}
        }

    def save_progress(self):
        """Save current progress to file"""
        self.progress_data["last_updated"] = datetime.now().isoformat()
        try:
            with open(self.tracking_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving progress: {e}")

    def scan_module_types(self):
        """Scan the module-types directory and build the initial list"""
        if not os.path.isdir(self.module_types_dir):
            print(f"Module-types directory not found: {self.module_types_dir}")
            return False

        patterns = ("**/*.yml", "**/*.yaml")
        files = []

        for pattern in patterns:
            files.extend(glob.glob(os.path.join(self.module_types_dir, pattern), recursive=True))

        if not files:
            print(f"No YAML files found under: {self.module_types_dir}")
            return False

        # Update progress data
        self.progress_data["total_files"] = len(files)

        for filepath in files:
            relative_path = os.path.relpath(filepath, self.module_types_dir)

            if relative_path not in self.progress_data["module_types"]:
                # Extract module info from YAML file
                module_info = self.extract_module_info(filepath)

                self.progress_data["module_types"][relative_path] = {
                    "full_path": filepath,
                    "manufacturer": module_info.get("manufacturer"),
                    "model": module_info.get("model"),
                    "slug": module_info.get("slug"),
                    "status": "pending",  # pending, created, failed, skipped
                    "created_at": None,
                    "error_message": None,
                    "netbox_id": None
                }

        self.save_progress()
        return True

    def extract_module_info(self, filepath):
        """Extract basic module information from YAML file"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)

            if data is None:
                return {}

            # Handle both single documents and lists
            if isinstance(data, list):
                data = data[0] if data else {}

            return {
                "manufacturer": data.get("manufacturer") or Path(filepath).parent.name,
                "model": data.get("model", ""),
                "slug": data.get("slug", ""),
                "description": data.get("description", "")
            }
        except Exception as e:
            print(f"Error reading {filepath}: {e}")
            return {}

    def get_pending_module_types(self):
        """Get list of module types that haven't been processed yet"""
        pending = []
        for relative_path, info in self.progress_data["module_types"].items():
            if info["status"] == "pending":
                pending.append((relative_path, info))
        return pending

    def mark_as_created(self, relative_path, netbox_id=None):
        """Mark a module type as successfully created"""
        if relative_path in self.progress_data["module_types"]:
            self.progress_data["module_types"][relative_path]["status"] = "created"
            self.progress_data["module_types"][relative_path]["created_at"] = datetime.now().isoformat()
            if netbox_id:
                self.progress_data["module_types"][relative_path]["netbox_id"] = netbox_id
            self.progress_data["processed_count"] = sum(1 for dt in self.progress_data["module_types"].values()
                                                       if dt["status"] == "created")
            self.save_progress()

    def mark_as_failed(self, relative_path, error_message):
        """Mark a module type as failed"""
        if relative_path in self.progress_data["module_types"]:
            self.progress_data["module_types"][relative_path]["status"] = "failed"
            self.progress_data["module_types"][relative_path]["error_message"] = error_message
            self.progress_data["failed_count"] = sum(1 for dt in self.progress_data["module_types"].values()
                                                     if dt["status"] == "failed")
            self.save_progress()

    def mark_as_skipped(self, relative_path, reason):
        """Mark a module type as skipped"""
        if relative_path in self.progress_data["module_types"]:
            self.progress_data["module_types"][relative_path]["status"] = "skipped"
            self.progress_data["module_types"][relative_path]["error_message"] = reason
            self.save_progress()

    def print_summary(self):
        """Print a summary of the current progress"""
        total = self.progress_data["total_files"]
        processed = self.progress_data["processed_count"]
        failed = self.progress_data["failed_count"]
        pending = len(self.get_pending_module_types())
        skipped = sum(1 for dt in self.progress_data["module_types"].values() if dt["status"] == "skipped")

        print(f"\n=== Module Type Processing Summary ===")
        print(f"Total module types found: {total}")
        print(f"Successfully created: {processed}")
        print(f"Failed: {failed}")
        print(f"Skipped: {skipped}")
        print(f"Pending: {pending}")
        print(f"Progress: {((processed + failed + skipped) / total * 100) if total else 0:.1f}%")
        print(f"Last updated: {self.progress_data['last_updated']}")

    def print_pending_list(self, limit=20):
        """Print list of pending module types"""
        pending = self.get_pending_module_types()

        print(f"\n=== Pending Module Types ({len(pending)} total) ===")
        for i, (relative_path, info) in enumerate(pending[:limit]):
            manufacturer = info.get("manufacturer", "Unknown")
            model = info.get("model", "Unknown")
            print(f"{i+1:3d}. {manufacturer} / {model}")
            print(f"     File: {relative_path}")

        if len(pending) > limit:
            print(f"     ... and {len(pending) - limit} more")

    def print_failed_list(self):
        """Print list of failed module types"""
        failed = [(path, info) for path, info in self.progress_data["module_types"].items()
                  if info["status"] == "failed"]

        if not failed:
            print("\n=== No Failed Module Types ===")
            return

        print(f"\n=== Failed Module Types ({len(failed)} total) ===")
        for i, (relative_path, info) in enumerate(failed):
            manufacturer = info.get("manufacturer", "Unknown")
            model = info.get("model", "Unknown")
            error = info.get("error_message", "Unknown error")
            print(f"{i+1:3d}. {manufacturer} / {model}")
            print(f"     File: {relative_path}")
            print(f"     Error: {error}")

    def reset_progress(self):
        """Reset all progress (use with caution!)"""
        response = input("Are you sure you want to reset all progress? (yes/no): ")
        if response.lower() == "yes":
            if os.path.exists(self.tracking_file):
                os.remove(self.tracking_file)
            self.progress_data = self.load_progress()
            print("Progress reset successfully!")
        else:
            print("Reset cancelled.")

def main():
    """Main function for command-line usage"""
    tracker = ModuleTypeTracker()

    print("Module Type Tracker")
    print("===================")

    # Scan for module types
    print("Scanning module-types directory...")
    if not tracker.scan_module_types():
        print("Failed to scan module-types directory.")
        return

    print("Scan completed!")

    while True:
        print("\nOptions:")
        print("1. Show summary")
        print("2. Show pending module types")
        print("3. Show failed module types")
        print("4. Reset progress (caution!)")
        print("5. Exit")

        choice = input("\nSelect an option (1-5): ").strip()

        if choice == "1":
            tracker.print_summary()
        elif choice == "2":
            tracker.print_pending_list()
        elif choice == "3":
            tracker.print_failed_list()
        elif choice == "4":
            tracker.reset_progress()
        elif choice == "5":
            break
        else:
            print("Invalid option. Please select 1-5.")

if __name__ == "__main__":
    main()