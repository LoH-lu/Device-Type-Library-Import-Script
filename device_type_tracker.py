import os
import glob
import yaml
import json
from pathlib import Path
from datetime import datetime

class DeviceTypeTracker:
    def __init__(self, base_dir=None):
        """
        Initialize the device type tracker
        
        Args:
            base_dir: Base directory where the script is located. If None, uses current directory.
        """
        self.base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self.device_types_dir = os.path.join(self.base_dir, "devicetype-library-master", "device-types")
        self.tracking_file = os.path.join(self.base_dir, "device_type_progress.json")
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
            "device_types": {}
        }
    
    def save_progress(self):
        """Save current progress to file"""
        self.progress_data["last_updated"] = datetime.now().isoformat()
        try:
            with open(self.tracking_file, 'w', encoding='utf-8') as f:
                json.dump(self.progress_data, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"Error saving progress: {e}")
    
    def scan_device_types(self):
        """Scan the device-types directory and build the initial list"""
        if not os.path.isdir(self.device_types_dir):
            print(f"Device-types directory not found: {self.device_types_dir}")
            return False
        
        patterns = ("**/*.yml", "**/*.yaml")
        files = []
        
        for pattern in patterns:
            files.extend(glob.glob(os.path.join(self.device_types_dir, pattern), recursive=True))
        
        if not files:
            print(f"No YAML files found under: {self.device_types_dir}")
            return False
        
        # Update progress data
        self.progress_data["total_files"] = len(files)
        
        for filepath in files:
            relative_path = os.path.relpath(filepath, self.device_types_dir)
            
            if relative_path not in self.progress_data["device_types"]:
                # Extract device info from YAML file
                device_info = self.extract_device_info(filepath)
                
                self.progress_data["device_types"][relative_path] = {
                    "full_path": filepath,
                    "manufacturer": device_info.get("manufacturer"),
                    "model": device_info.get("model"),
                    "slug": device_info.get("slug"),
                    "status": "pending",  # pending, created, failed, skipped
                    "created_at": None,
                    "error_message": None,
                    "netbox_id": None
                }
        
        self.save_progress()
        return True
    
    def extract_device_info(self, filepath):
        """Extract basic device information from YAML file"""
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
    
    def get_pending_device_types(self):
        """Get list of device types that haven't been processed yet"""
        pending = []
        for relative_path, info in self.progress_data["device_types"].items():
            if info["status"] == "pending":
                pending.append((relative_path, info))
        return pending
    
    def mark_as_created(self, relative_path, netbox_id=None):
        """Mark a device type as successfully created"""
        if relative_path in self.progress_data["device_types"]:
            self.progress_data["device_types"][relative_path]["status"] = "created"
            self.progress_data["device_types"][relative_path]["created_at"] = datetime.now().isoformat()
            if netbox_id:
                self.progress_data["device_types"][relative_path]["netbox_id"] = netbox_id
            self.progress_data["processed_count"] = sum(1 for dt in self.progress_data["device_types"].values() 
                                                     if dt["status"] == "created")
            self.save_progress()
    
    def mark_as_failed(self, relative_path, error_message):
        """Mark a device type as failed"""
        if relative_path in self.progress_data["device_types"]:
            self.progress_data["device_types"][relative_path]["status"] = "failed"
            self.progress_data["device_types"][relative_path]["error_message"] = error_message
            self.progress_data["failed_count"] = sum(1 for dt in self.progress_data["device_types"].values() 
                                                   if dt["status"] == "failed")
            self.save_progress()
    
    def mark_as_skipped(self, relative_path, reason):
        """Mark a device type as skipped"""
        if relative_path in self.progress_data["device_types"]:
            self.progress_data["device_types"][relative_path]["status"] = "skipped"
            self.progress_data["device_types"][relative_path]["error_message"] = reason
            self.save_progress()
    
    def print_summary(self):
        """Print a summary of the current progress"""
        total = self.progress_data["total_files"]
        processed = self.progress_data["processed_count"]
        failed = self.progress_data["failed_count"]
        pending = len(self.get_pending_device_types())
        skipped = sum(1 for dt in self.progress_data["device_types"].values() if dt["status"] == "skipped")
        
        print(f"\n=== Device Type Processing Summary ===")
        print(f"Total device types found: {total}")
        print(f"Successfully created: {processed}")
        print(f"Failed: {failed}")
        print(f"Skipped: {skipped}")
        print(f"Pending: {pending}")
        print(f"Progress: {((processed + failed + skipped) / total * 100):.1f}%")
        print(f"Last updated: {self.progress_data['last_updated']}")
    
    def print_pending_list(self, limit=20):
        """Print list of pending device types"""
        pending = self.get_pending_device_types()
        
        print(f"\n=== Pending Device Types ({len(pending)} total) ===")
        for i, (relative_path, info) in enumerate(pending[:limit]):
            manufacturer = info.get("manufacturer", "Unknown")
            model = info.get("model", "Unknown")
            print(f"{i+1:3d}. {manufacturer} / {model}")
            print(f"     File: {relative_path}")
        
        if len(pending) > limit:
            print(f"     ... and {len(pending) - limit} more")
    
    def print_failed_list(self):
        """Print list of failed device types"""
        failed = [(path, info) for path, info in self.progress_data["device_types"].items() 
                 if info["status"] == "failed"]
        
        if not failed:
            print("\n=== No Failed Device Types ===")
            return
        
        print(f"\n=== Failed Device Types ({len(failed)} total) ===")
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
    tracker = DeviceTypeTracker()
    
    print("Device Type Tracker")
    print("==================")
    
    # Scan for device types
    print("Scanning device-types directory...")
    if not tracker.scan_device_types():
        print("Failed to scan device types directory.")
        return
    
    print("Scan completed!")
    
    while True:
        print("\nOptions:")
        print("1. Show summary")
        print("2. Show pending device types")
        print("3. Show failed device types")
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